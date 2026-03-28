"""Schedule evaluator — background task that triggers playback on devices."""

import asyncio
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from cms import database as _db
from cms.models.asset import Asset
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.setting import CMSSetting
from cms.schemas.protocol import FetchAssetMessage, PlayMessage, StopMessage
from cms.services.device_manager import device_manager

logger = logging.getLogger("agora.cms.scheduler")

# Track what each device is currently playing via scheduler, to avoid spamming
# Key: device_id, Value: (schedule_id, asset_id)
_active_playback: dict[str, tuple[str, str]] = {}

# Rich now-playing info for the dashboard
# Key: device_id, Value: dict with schedule_name, asset_filename, device_name, since
_now_playing: dict[str, dict] = {}

# Track which assets have been pre-fetched to avoid re-sending
# Key: (device_id, schedule_id, asset_id)
_prefetched: set[tuple[str, str, str]] = set()

EVAL_INTERVAL_SECONDS = 15
PREFETCH_MINUTES = 5


def get_now_playing() -> list[dict]:
    """Return a list of currently active schedule playbacks for the dashboard."""
    return list(_now_playing.values())


def _matches_now(schedule: Schedule, now: datetime) -> bool:
    """Check if a schedule is active at the given datetime."""
    if not schedule.enabled:
        return False

    # Date range check
    if schedule.start_date and now < schedule.start_date:
        return False
    if schedule.end_date and now > schedule.end_date:
        return False

    # Day of week check (ISO: 1=Mon, 7=Sun)
    if schedule.days_of_week:
        iso_day = now.isoweekday()
        if iso_day not in schedule.days_of_week:
            return False

    # Time window check
    current_time = now.time()
    if schedule.start_time <= schedule.end_time:
        # Normal range: e.g. 09:00–17:00
        if not (schedule.start_time <= current_time <= schedule.end_time):
            return False
    else:
        # Overnight range: e.g. 22:00–06:00
        if not (current_time >= schedule.start_time or current_time <= schedule.end_time):
            return False

    return True


def _starts_within(schedule: Schedule, now: datetime, minutes: int) -> bool:
    """Check if a schedule starts within the next N minutes (but is NOT active yet)."""
    if not schedule.enabled:
        return False

    # Date range check
    if schedule.start_date and now < schedule.start_date:
        # Schedule hasn't started date-wise yet — but might start within minutes today
        # Only pre-fetch if start_date is today or earlier
        if now.date() < schedule.start_date.date():
            return False
    if schedule.end_date and now > schedule.end_date:
        return False

    # Day of week check
    if schedule.days_of_week:
        iso_day = now.isoweekday()
        if iso_day not in schedule.days_of_week:
            return False

    # Check if start_time is within [now, now + minutes]
    current_time = now.time()
    future = (now + timedelta(minutes=minutes)).time()

    # Skip if already active
    if _matches_now(schedule, now):
        return False

    # Check if start_time falls in the lookahead window
    if current_time <= future:
        # No midnight wrap in lookahead window
        return current_time <= schedule.start_time <= future
    else:
        # Lookahead window wraps midnight
        return schedule.start_time >= current_time or schedule.start_time <= future


async def _get_target_device_ids(schedule: Schedule, db) -> list[str]:
    """Resolve target device IDs for a schedule."""
    if schedule.device_id:
        return [schedule.device_id]
    elif schedule.group_id:
        result = await db.execute(
            select(Device.id).where(
                Device.group_id == schedule.group_id,
                Device.status == DeviceStatus.APPROVED,
            )
        )
        return [row[0] for row in result.all()]
    return []


def _get_base_url_from_connections() -> str:
    """Derive CMS base URL from an active WebSocket connection."""
    for did in device_manager.connected_ids:
        conn = device_manager.get(did)
        if conn:
            ws_url = conn.websocket.url
            scheme = "https" if ws_url.scheme == "wss" else "http"
            base_url = f"{scheme}://{ws_url.hostname}"
            if ws_url.port and ws_url.port not in (80, 443):
                base_url += f":{ws_url.port}"
            return base_url
    return "http://0.0.0.0:8080"


async def _fetch_asset_to_device(device_id: str, asset: Asset, base_url: str) -> None:
    """Send fetch_asset only (no play) — for pre-fetching."""
    download_url = f"{base_url}/api/assets/{asset.id}/download"
    fetch = FetchAssetMessage(
        asset_name=asset.filename,
        download_url=download_url,
        checksum=asset.checksum,
        size_bytes=asset.size_bytes,
    )
    await device_manager.send_to_device(device_id, fetch.model_dump(mode="json"))


async def _push_asset_and_play(device_id: str, asset: Asset, base_url: str) -> None:
    """Send fetch_asset then play to a device."""
    await _fetch_asset_to_device(device_id, asset, base_url)
    play = PlayMessage(asset=asset.filename, loop=True)
    await device_manager.send_to_device(device_id, play.model_dump(mode="json"))


async def evaluate_schedules() -> None:
    """Single evaluation pass: determine what each connected device should play."""
    if not device_manager.connected_count:
        return

    if _db._session_factory is None:
        return

    now = datetime.now(timezone.utc)

    async with _db._session_factory() as db:
        # Read configured timezone
        tz_result = await db.execute(
            select(CMSSetting.value).where(CMSSetting.key == "timezone")
        )
        tz_name = tz_result.scalar_one_or_none() or "UTC"
        local_now = now.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)

        # Load all enabled schedules with their relationships
        result = await db.execute(
            select(Schedule)
            .options(
                selectinload(Schedule.asset),
                selectinload(Schedule.device),
                selectinload(Schedule.group),
            )
            .where(Schedule.enabled == True)  # noqa: E712
        )
        schedules = result.scalars().all()

        base_url = _get_base_url_from_connections()

        # ── Phase 1: Pre-fetch assets for upcoming schedules ──
        for schedule in schedules:
            if not schedule.asset:
                continue
            if not _starts_within(schedule, local_now, PREFETCH_MINUTES):
                continue

            target_ids = await _get_target_device_ids(schedule, db)
            for did in target_ids:
                if not device_manager.is_connected(did):
                    continue
                pf_key = (did, str(schedule.id), str(schedule.asset.id))
                if pf_key in _prefetched:
                    continue

                logger.info(
                    "Pre-fetching '%s' to device %s for schedule '%s' (starts at %s)",
                    schedule.asset.filename, did, schedule.name, schedule.start_time,
                )
                await _fetch_asset_to_device(did, schedule.asset, base_url)
                _prefetched.add(pf_key)

        # ── Phase 2: Evaluate active schedules and trigger playback ──
        active = [s for s in schedules if _matches_now(s, local_now)]

        # Build device → winning schedule map (highest priority wins)
        device_schedule: dict[str, tuple[Schedule, Asset]] = {}

        for schedule in active:
            if not schedule.asset:
                continue

            target_ids = await _get_target_device_ids(schedule, db)
            for did in target_ids:
                if not device_manager.is_connected(did):
                    continue
                existing = device_schedule.get(did)
                if existing is None or schedule.priority > existing[0].priority:
                    device_schedule[did] = (schedule, schedule.asset)

        # Act on each connected device
        connected = set(device_manager.connected_ids)

        # Load device names for now-playing display
        device_names: dict[str, str] = {}
        if connected:
            name_q = await db.execute(
                select(Device.id, Device.name).where(Device.id.in_(connected))
            )
            device_names = {row[0]: (row[1] or row[0]) for row in name_q.all()}

        for did in connected:
            winner = device_schedule.get(did)

            if winner:
                schedule, asset = winner
                key = (str(schedule.id), str(asset.id))

                if _active_playback.get(did) != key:
                    logger.info(
                        "Schedule '%s' → device %s: playing %s (priority %d)",
                        schedule.name, did, asset.filename, schedule.priority,
                    )
                    await _push_asset_and_play(did, asset, base_url)
                    _active_playback[did] = key

                # Always update now-playing (keeps "since" fresh on first set)
                if did not in _now_playing or _now_playing[did].get("schedule_id") != str(schedule.id):
                    _now_playing[did] = {
                        "device_id": did,
                        "device_name": device_names.get(did, did),
                        "schedule_id": str(schedule.id),
                        "schedule_name": schedule.name,
                        "asset_filename": asset.filename,
                        "since": now.isoformat(),
                    }
            else:
                # No active schedule — if we were previously driving this device,
                # stop and let it fall back to default/splash
                if did in _active_playback:
                    logger.info("No active schedule for device %s, stopping", did)
                    stop = StopMessage()
                    await device_manager.send_to_device(did, stop.model_dump(mode="json"))
                    del _active_playback[did]
                _now_playing.pop(did, None)

        # Clean up stale prefetch entries (older than 1 hour to avoid memory leak)
        # We keep them around so we don't re-prefetch the same schedule repeatedly
        # They'll naturally stop matching _starts_within once the schedule is active


async def scheduler_loop() -> None:
    """Background loop that periodically evaluates schedules."""
    logger.info("Scheduler started (interval=%ds)", EVAL_INTERVAL_SECONDS)
    while True:
        try:
            await evaluate_schedules()
        except Exception:
            logger.exception("Scheduler evaluation error")
        await asyncio.sleep(EVAL_INTERVAL_SECONDS)
