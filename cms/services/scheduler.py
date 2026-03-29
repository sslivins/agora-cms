"""Schedule evaluator — background task that syncs schedules to devices."""

import asyncio
import hashlib
import logging
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from cms import database as _db
from cms.models.asset import Asset, AssetVariant, VariantStatus
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.setting import CMSSetting
from cms.schemas.protocol import ScheduleEntry, SyncMessage
from cms.services.device_manager import device_manager

logger = logging.getLogger("agora.cms.scheduler")

# Track last sync hash per device to avoid re-sending identical syncs
_last_sync_hash: dict[str, str] = {}

# Rich now-playing info for the dashboard
_now_playing: dict[str, dict] = {}

EVAL_INTERVAL_SECONDS = 15
SCHEDULE_WINDOW_DAYS = 30


def get_now_playing() -> list[dict]:
    """Return a list of currently active schedule playbacks for the dashboard."""
    return list(_now_playing.values())


def _matches_now(schedule: Schedule, now: datetime) -> bool:
    """Check if a schedule is active at the given datetime."""
    if not schedule.enabled:
        return False
    if schedule.start_date and now < schedule.start_date:
        return False
    if schedule.end_date and now > schedule.end_date:
        return False
    if schedule.days_of_week:
        if now.isoweekday() not in schedule.days_of_week:
            return False
    current_time = now.time()
    if schedule.start_time <= schedule.end_time:
        if not (schedule.start_time <= current_time < schedule.end_time):
            return False
    else:
        if not (current_time >= schedule.start_time or current_time < schedule.end_time):
            return False
    return True


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


def _schedule_to_entry(s: Schedule, variant_checksums: dict[str, str] | None = None) -> ScheduleEntry:
    """Convert a Schedule ORM model to a protocol ScheduleEntry."""
    checksum = None
    if variant_checksums and s.asset.filename in variant_checksums:
        checksum = variant_checksums[s.asset.filename]
    elif s.asset:
        checksum = s.asset.checksum or None
    return ScheduleEntry(
        id=str(s.id),
        name=s.name,
        asset=s.asset.filename,
        asset_checksum=checksum,
        start_time=s.start_time.strftime("%H:%M"),
        end_time=s.end_time.strftime("%H:%M"),
        start_date=s.start_date.date().isoformat() if s.start_date else None,
        end_date=s.end_date.date().isoformat() if s.end_date else None,
        days_of_week=s.days_of_week,
        priority=s.priority,
    )


def _sync_hash(sync: SyncMessage) -> str:
    """Compute a hash of a sync message for dedup."""
    return hashlib.md5(sync.model_dump_json().encode()).hexdigest()


async def build_device_sync(device_id: str, db) -> SyncMessage | None:
    """Build a full SyncMessage for a specific device.

    Used by both the scheduler loop and the on-change push.
    Returns None if the database isn't ready.
    """
    # Read configured timezone
    tz_result = await db.execute(
        select(CMSSetting.value).where(CMSSetting.key == "timezone")
    )
    tz_name = tz_result.scalar_one_or_none() or "UTC"

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=SCHEDULE_WINDOW_DAYS)

    # Load device with default asset
    dev_result = await db.execute(
        select(Device)
        .options(
            selectinload(Device.default_asset),
            selectinload(Device.group).selectinload(DeviceGroup.default_asset),
        )
        .where(Device.id == device_id)
    )
    dev = dev_result.scalar_one_or_none()
    if not dev:
        return None

    # Resolve default asset (device → group fallback)
    default_asset_name = None
    default_asset_checksum = None
    if dev.default_asset:
        default_asset_name = dev.default_asset.filename
        default_asset_checksum = dev.default_asset.checksum
    elif dev.group and dev.group.default_asset:
        default_asset_name = dev.group.default_asset.filename
        default_asset_checksum = dev.group.default_asset.checksum

    # Build variant checksum map for this device's profile
    # (maps source asset filename → variant checksum)
    variant_checksums: dict[str, str] = {}
    if dev.profile_id:
        var_result = await db.execute(
            select(AssetVariant)
            .options(selectinload(AssetVariant.source_asset))
            .where(
                AssetVariant.profile_id == dev.profile_id,
                AssetVariant.status == VariantStatus.READY,
            )
        )
        for v in var_result.scalars().all():
            variant_checksums[v.source_asset.filename] = v.checksum
        # Also override default_asset_checksum if there's a variant
        if default_asset_name and default_asset_name in variant_checksums:
            default_asset_checksum = variant_checksums[default_asset_name]

    # Load all enabled schedules targeting this device (directly or via group)
    result = await db.execute(
        select(Schedule)
        .options(
            selectinload(Schedule.asset),
            selectinload(Schedule.device),
            selectinload(Schedule.group),
        )
        .where(Schedule.enabled == True)  # noqa: E712
    )
    all_schedules = result.scalars().all()

    # Filter to schedules that target this device and are within the window
    entries: list[ScheduleEntry] = []
    for s in all_schedules:
        if not s.asset:
            continue
        # Check date range — skip if entirely in the past or beyond the window
        if s.end_date and s.end_date < now:
            continue
        if s.start_date and s.start_date > cutoff:
            continue

        target_ids = await _get_target_device_ids(s, db)
        if device_id in target_ids:
            entries.append(_schedule_to_entry(s, variant_checksums))

    return SyncMessage(
        timezone=tz_name,
        schedules=entries,
        default_asset=default_asset_name,
        default_asset_checksum=default_asset_checksum or None,
    )


async def push_sync_to_device(device_id: str, db) -> None:
    """Build and push a fresh sync to a single connected device."""
    if not device_manager.is_connected(device_id):
        return

    sync = await build_device_sync(device_id, db)
    if sync is None:
        return

    h = _sync_hash(sync)
    if _last_sync_hash.get(device_id) == h:
        return

    await device_manager.send_to_device(device_id, sync.model_dump(mode="json"))
    _last_sync_hash[device_id] = h
    logger.info("Pushed full sync to device %s (%d schedules)", device_id, len(sync.schedules))


async def push_sync_to_affected_devices(schedule: Schedule, db) -> None:
    """Push sync to all devices affected by a schedule change."""
    target_ids = await _get_target_device_ids(schedule, db)
    for did in target_ids:
        await push_sync_to_device(did, db)


async def evaluate_schedules() -> None:
    """Single evaluation pass: sync schedules and update now-playing dashboard."""
    if not device_manager.connected_count:
        return

    if _db._session_factory is None:
        return

    now = datetime.now(timezone.utc)

    async with _db._session_factory() as db:
        # Read timezone for now-playing evaluation
        tz_result = await db.execute(
            select(CMSSetting.value).where(CMSSetting.key == "timezone")
        )
        tz_name = tz_result.scalar_one_or_none() or "UTC"
        local_now = now.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)

        connected = set(device_manager.connected_ids)

        # Push full sync to each connected device (dedup via hash)
        for did in connected:
            await push_sync_to_device(did, db)

        # ── Update now-playing dashboard ──
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

        active = [s for s in schedules if _matches_now(s, local_now)]

        # Find winner per device (highest priority)
        device_winner: dict[str, Schedule] = {}
        for s in active:
            if not s.asset:
                continue
            target_ids = await _get_target_device_ids(s, db)
            for did in target_ids:
                if did not in connected:
                    continue
                existing = device_winner.get(did)
                if existing is None or s.priority > existing.priority:
                    device_winner[did] = s

        # Load device names
        device_names: dict[str, str] = {}
        if connected:
            name_q = await db.execute(
                select(Device.id, Device.name).where(Device.id.in_(connected))
            )
            device_names = {r[0]: (r[1] or r[0]) for r in name_q.all()}

        for did in connected:
            winner = device_winner.get(did)
            if winner:
                if did not in _now_playing or _now_playing[did].get("schedule_id") != str(winner.id):
                    _now_playing[did] = {
                        "device_id": did,
                        "device_name": device_names.get(did, did),
                        "schedule_id": str(winner.id),
                        "schedule_name": winner.name,
                        "asset_filename": winner.asset.filename,
                        "since": now.isoformat(),
                    }
            else:
                _now_playing.pop(did, None)


async def scheduler_loop() -> None:
    """Background loop that periodically evaluates schedules."""
    logger.info("Scheduler started (interval=%ds)", EVAL_INTERVAL_SECONDS)
    while True:
        try:
            await evaluate_schedules()
        except Exception:
            logger.exception("Scheduler evaluation error")
        await asyncio.sleep(EVAL_INTERVAL_SECONDS)
