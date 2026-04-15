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
from cms.models.schedule_log import ScheduleLog, ScheduleLogEvent
from cms.models.setting import CMSSetting
from cms.schemas.protocol import ScheduleEntry, SyncMessage
from cms.services.device_manager import device_manager

logger = logging.getLogger("agora.cms.scheduler")

# Track last sync hash per device to avoid re-sending identical syncs
_last_sync_hash: dict[str, str] = {}

# Rich now-playing info for the dashboard
_now_playing: dict[str, dict] = {}

# Skipped schedule occurrences: {schedule_id: skip_until_local_datetime}
_skipped: dict[str, datetime] = {}

# Track which schedule+device combos we've already logged as MISSED this eval cycle
# Key: (schedule_id, device_id), cleared when the schedule/device combo resolves
_missed_logged: set[tuple[str, str]] = set()

# Track when a (schedule_id, device_id) combo was first seen offline
# Only log MISSED after MISSED_GRACE_SECONDS of continuous offline
_offline_since: dict[tuple[str, str], datetime] = {}

EVAL_INTERVAL_SECONDS = 15
MISSED_GRACE_SECONDS = 60
SCHEDULE_WINDOW_DAYS = 30


async def _log_event(db, event: ScheduleLogEvent, schedule_name: str, device_name: str,
                     asset_filename: str, schedule_id=None, device_id=None, details=None):
    """Write a schedule history log entry."""
    import uuid as _uuid
    from sqlalchemy.exc import IntegrityError
    # Ensure schedule_id is a proper UUID (may arrive as string from _now_playing)
    if isinstance(schedule_id, str):
        try:
            schedule_id = _uuid.UUID(schedule_id)
        except ValueError:
            schedule_id = None
    entry = ScheduleLog(
        schedule_id=schedule_id,
        schedule_name=schedule_name,
        device_id=device_id,
        device_name=device_name,
        asset_filename=asset_filename,
        event=event,
        details=details,
    )
    db.add(entry)
    try:
        await db.flush()
    except IntegrityError:
        # FK target was deleted (schedule or device removed while playing).
        # Retry without the dangling FK — the denormalized name columns
        # still preserve the context for the log entry.
        await db.rollback()
        entry = ScheduleLog(
            schedule_id=None,
            schedule_name=schedule_name,
            device_id=None,
            device_name=device_name,
            asset_filename=asset_filename,
            event=event,
            details=details,
        )
        db.add(entry)
        await db.flush()


# Public alias for use by ws.py handler
log_schedule_event = _log_event


def get_now_playing() -> list[dict]:
    """Return a list of currently active schedule playbacks for the dashboard.

    Returns shallow copies so callers (dashboard routes) can annotate entries
    with transient keys like ``mismatch`` / ``starting`` without polluting
    the canonical scheduler state.
    """
    return [d.copy() for d in _now_playing.values()]


def set_now_playing(device_id: str, entry: dict) -> None:
    """Set the now-playing entry for a device (called by WS handler on PLAYBACK_STARTED)."""
    _now_playing[device_id] = entry


def clear_now_playing(device_id: str) -> dict | None:
    """Remove and return the now-playing entry for a device (called by WS handler on PLAYBACK_ENDED)."""
    return _now_playing.pop(device_id, None)


def skip_schedule_until(schedule_id: str, until: datetime) -> None:
    """Skip a schedule's current occurrence until the given local datetime."""
    _skipped[schedule_id] = until
    # Remove from now_playing immediately
    to_remove = [did for did, info in _now_playing.items() if info.get("schedule_id") == schedule_id]
    for did in to_remove:
        _now_playing.pop(did, None)


def clear_schedule_skip(schedule_id: str) -> None:
    """Remove any active skip for a schedule so it can be re-evaluated."""
    _skipped.pop(schedule_id, None)


def clear_sync_hash(device_id: str) -> None:
    """Clear the cached sync hash for a device so the next eval re-sends."""
    _last_sync_hash.pop(device_id, None)


def get_upcoming_schedules(
    schedules: list, now: datetime, tz: ZoneInfo,
    now_playing: list[dict] | None = None,
    offline_device_ids: set[str] | None = None,
) -> list[dict]:
    """Return schedules starting within the next 24 hours.

    Schedules currently in their time window but preempted by a higher-priority
    schedule on the same target are included with ``preempted=True`` and a
    ``resumes_at`` hint when possible.

    Each entry includes start/end time, duration, countdown, and whether it's
    today or tomorrow.
    """
    _offline = offline_device_ids or set()
    local_now = now.astimezone(tz).replace(tzinfo=None)
    today = local_now.date()
    tomorrow = today + timedelta(days=1)
    results = []

    # Build set of currently-winning schedule IDs from now_playing
    _winning_sids: set[str] = set()
    _winning_by_did: dict[str, dict] = {}  # device_id → now_playing entry
    if now_playing is not None:
        _winning_sids = {np["schedule_id"] for np in now_playing}
        for np in now_playing:
            _winning_by_did[np["device_id"]] = np

    # Build a priority lookup for the currently-winning schedules
    _sched_priority: dict[str, int] = {}
    for sched in schedules:
        _sched_priority[str(sched.id)] = sched.priority

    for s in schedules:
        if not s.enabled:
            continue
        if _matches_now(s, local_now):
            if now_playing is None:
                continue  # legacy: no preemption info available
            if str(s.id) in _winning_sids:
                continue  # currently winning — shown in Now Playing
            if str(s.id) in _skipped:
                continue  # deliberately ended via End Now
            # Check if genuinely preempted (higher-priority schedule active on same target)
            resume_at = _find_resume_time(s, schedules, local_now)
            if resume_at is not None:
                results.append(_preempted_entry(s, local_now, resume_at))
            else:
                # No winner yet for this target — scheduler
                # hasn't evaluated.  Show as "starting" so the schedule
                # doesn't vanish from the dashboard during the transition.
                entry = _starting_entry(s, local_now)
                results.append(entry)
            continue

        # Check today
        today_start = datetime.combine(today, s.start_time)
        if today_start > local_now:
            if s.start_date and today < s.start_date.date():
                pass  # hasn't started yet
            elif s.end_date and today > s.end_date.date():
                pass  # already ended
            elif s.days_of_week and local_now.isoweekday() not in s.days_of_week:
                pass  # not scheduled today
            else:
                delta = today_start - local_now
                results.append(_upcoming_entry(s, today, "today", delta))
                continue

        # Check tomorrow
        tomorrow_start = datetime.combine(tomorrow, s.start_time)
        delta = tomorrow_start - local_now
        if delta.total_seconds() > 86400:
            continue
        if s.start_date and tomorrow < s.start_date.date():
            continue
        if s.end_date and tomorrow > s.end_date.date():
            continue
        if s.days_of_week and tomorrow.isoweekday() not in s.days_of_week:
            continue
        results.append(_upcoming_entry(s, tomorrow, "tomorrow", delta))

    results.sort(key=lambda e: e["starts_in_seconds"])
    return results


def _upcoming_entry(s: Schedule, run_date, day_label: str, delta: timedelta) -> dict:
    """Build an upcoming schedule entry dict."""
    start_dt = datetime.combine(run_date, s.start_time)
    end_dt = datetime.combine(run_date, s.end_time)
    if s.end_time <= s.start_time:
        end_dt += timedelta(days=1)
    duration_mins = int((end_dt - start_dt).total_seconds() / 60)
    duration_secs = int((end_dt - start_dt).total_seconds())

    total_secs = int(delta.total_seconds())
    if total_secs < 60:
        countdown = "less than a minute"
    elif total_secs < 3600:
        mins = total_secs // 60
        countdown = f"{mins} minute{'s' if mins != 1 else ''}"
    else:
        hours = total_secs // 3600
        mins = (total_secs % 3600) // 60
        countdown = f"{hours} hour{'s' if hours != 1 else ''}"
        if mins > 0:
            countdown += f", {mins} minute{'s' if mins != 1 else ''}"

    target_name = s.group.name if s.group else None

    return {
        "schedule_name": s.name,
        "asset_filename": s.asset.filename if s.asset else "—",
        "target_name": target_name or "—",
        "target_type": "group",
        "start_time": s.start_time.strftime("%I:%M %p").lstrip("0"),
        "end_time": s.end_time.strftime("%I:%M %p").lstrip("0"),
        "duration_mins": duration_mins,
        "duration_secs": duration_secs,
        "countdown": countdown,
        "starts_in_seconds": total_secs,
        "day_label": day_label,
    }


def _find_resume_time(preempted: Schedule, all_schedules: list, local_now: datetime) -> time | None:
    """Find when a preempted schedule will resume playing.

    Returns the end_time of the latest-ending higher-priority schedule that is
    currently active on the same target, or ``None`` if no preempting schedule
    can be identified (e.g. device offline, cross-target preemption).
    """
    latest_end_dt = None
    for other in all_schedules:
        if other.id == preempted.id or not other.enabled:
            continue
        if other.priority <= preempted.priority:
            continue
        if not _matches_now(other, local_now):
            continue
        # Must target the same group
        same_target = (
            preempted.group_id and other.group_id == preempted.group_id
        )
        if not same_target:
            continue
        end_dt = datetime.combine(local_now.date(), other.end_time)
        if other.end_time <= other.start_time:
            end_dt += timedelta(days=1)
        if latest_end_dt is None or end_dt > latest_end_dt:
            latest_end_dt = end_dt
    return latest_end_dt.time() if latest_end_dt else None


def _preempted_entry(s: Schedule, local_now: datetime, resume_at: time) -> dict:
    """Build an entry for a schedule that is currently preempted."""
    start_dt = datetime.combine(local_now.date(), s.start_time)
    end_dt = datetime.combine(local_now.date(), s.end_time)
    if s.end_time <= s.start_time:
        end_dt += timedelta(days=1)
    duration_mins = int((end_dt - start_dt).total_seconds() / 60)
    duration_secs = int((end_dt - start_dt).total_seconds())

    target_name = s.group.name if s.group else None

    resume_dt = datetime.combine(local_now.date(), resume_at)
    if resume_at <= local_now.time():
        resume_dt += timedelta(days=1)
    resume_secs = max(0, int((resume_dt - local_now).total_seconds()))

    if resume_secs < 60:
        countdown = "resumes in less than a minute"
    elif resume_secs < 3600:
        mins = resume_secs // 60
        countdown = f"resumes in {mins} minute{'s' if mins != 1 else ''}"
    else:
        hours = resume_secs // 3600
        mins = (resume_secs % 3600) // 60
        countdown = f"resumes in {hours} hour{'s' if hours != 1 else ''}"
        if mins > 0:
            countdown += f", {mins} minute{'s' if mins != 1 else ''}"

    return {
        "schedule_name": s.name,
        "asset_filename": s.asset.filename if s.asset else "—",
        "target_name": target_name or "—",
        "target_type": "group",
        "start_time": s.start_time.strftime("%I:%M %p").lstrip("0"),
        "end_time": s.end_time.strftime("%I:%M %p").lstrip("0"),
        "duration_mins": duration_mins,
        "duration_secs": duration_secs,
        "countdown": countdown,
        "starts_in_seconds": resume_secs,
        "day_label": "today",
        "preempted": True,
        "resumes_at": resume_at.strftime("%I:%M %p").lstrip("0"),
    }


def _starting_entry(s: Schedule, local_now: datetime) -> dict:
    """Build an entry for a schedule that is in its window but not yet in now_playing."""
    start_dt = datetime.combine(local_now.date(), s.start_time)
    end_dt = datetime.combine(local_now.date(), s.end_time)
    if s.end_time <= s.start_time:
        end_dt += timedelta(days=1)
    duration_mins = int((end_dt - start_dt).total_seconds() / 60)
    duration_secs = int((end_dt - start_dt).total_seconds())

    target_name = s.group.name if s.group else None

    return {
        "schedule_name": s.name,
        "asset_filename": s.asset.filename if s.asset else "—",
        "target_name": target_name or "—",
        "target_type": "group",
        "start_time": s.start_time.strftime("%I:%M %p").lstrip("0"),
        "end_time": s.end_time.strftime("%I:%M %p").lstrip("0"),
        "duration_mins": duration_mins,
        "duration_secs": duration_secs,
        "countdown": "starting",
        "starts_in_seconds": 0,
        "day_label": "today",
        "starting": True,
    }


def _matches_now(schedule: Schedule, now: datetime) -> bool:
    """Check if a schedule is active at the given datetime."""
    if not schedule.enabled:
        return False
    # Compare dates only (not timestamps) — start_date/end_date represent whole days
    now_date = now.date() if hasattr(now, 'date') else now
    if schedule.start_date:
        start_d = schedule.start_date.date() if hasattr(schedule.start_date, 'date') else schedule.start_date
        if now_date < start_d:
            return False
    if schedule.end_date:
        end_d = schedule.end_date.date() if hasattr(schedule.end_date, 'date') else schedule.end_date
        if now_date > end_d:
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


def _times_overlap(s1: time, e1: time, s2: time, e2: time) -> bool:
    """Check if two time intervals overlap on a 24-hour clock.

    Handles overnight spans (e.g. 22:00–06:00).
    Zero-length windows (start == end) never overlap.
    """
    def to_min(t: time) -> int:
        return t.hour * 60 + t.minute

    a, b = to_min(s1), to_min(e1)
    c, d = to_min(s2), to_min(e2)

    if a == b or c == d:
        return False

    # Both non-wrapping
    if a < b and c < d:
        return a < d and c < b

    # Both wrap around midnight — always overlap
    if a >= b and c >= d:
        return True

    # One wraps, one doesn't — normalize so (a,b) wraps
    if c >= d:
        a, b, c, d = c, d, a, b

    # (a,b) wraps: covers [a,1440) ∪ [0,b). (c,d) doesn't wrap: [c,d)
    return c < b or d > a


def _days_overlap(d1: list[int] | None, d2: list[int] | None) -> bool:
    """Check if two days-of-week sets share any day. None means every day."""
    if not d1 or not d2:
        return True
    return bool(set(d1) & set(d2))


def _dates_overlap(
    s1: datetime | None, e1: datetime | None,
    s2: datetime | None, e2: datetime | None,
) -> bool:
    """Check if two date ranges overlap. None means unbounded."""
    if e1 and s2:
        e1d = e1.date() if hasattr(e1, 'date') else e1
        s2d = s2.date() if hasattr(s2, 'date') else s2
        if e1d < s2d:
            return False
    if e2 and s1:
        e2d = e2.date() if hasattr(e2, 'date') else e2
        s1d = s1.date() if hasattr(s1, 'date') else s1
        if e2d < s1d:
            return False
    return True


def schedules_conflict(a: Schedule, b: Schedule) -> bool:
    """Check if two schedules conflict (same target, same priority, overlapping windows)."""
    # Must share the same target
    if a.group_id and b.group_id:
        if a.group_id != b.group_id:
            return False
    else:
        return False  # No shared target

    if a.priority != b.priority:
        return False

    return (
        _times_overlap(a.start_time, a.end_time, b.start_time, b.end_time)
        and _days_overlap(a.days_of_week, b.days_of_week)
        and _dates_overlap(a.start_date, a.end_date, b.start_date, b.end_date)
    )


async def _get_target_device_ids(schedule: Schedule, db) -> list[str]:
    """Resolve target device IDs for a schedule's group."""
    if schedule.group_id:
        result = await db.execute(
            select(Device.id).where(
                Device.group_id == schedule.group_id,
                Device.status == DeviceStatus.ADOPTED,
            )
        )
        return [row[0] for row in result.all()]
    return []


def _schedule_to_entry(s: Schedule, variant_checksums: dict[str, str] | None = None) -> ScheduleEntry:
    """Convert a Schedule ORM model to a protocol ScheduleEntry."""
    from shared.models.asset import AssetType
    checksum = None
    if variant_checksums and s.asset.filename in variant_checksums:
        checksum = variant_checksums[s.asset.filename]
    elif s.asset:
        checksum = s.asset.checksum or None

    # For webpage assets, include the URL and skip the checksum
    is_webpage = s.asset and s.asset.asset_type == AssetType.WEBPAGE
    return ScheduleEntry(
        id=str(s.id),
        name=s.name,
        asset=s.asset.filename,
        asset_checksum=None if is_webpage else checksum,
        asset_type=s.asset.asset_type.value if s.asset else None,
        url=s.asset.url if is_webpage else None,
        start_time=s.start_time.strftime("%H:%M:%S"),
        end_time=s.end_time.strftime("%H:%M:%S"),
        start_date=s.start_date.date().isoformat() if s.start_date else None,
        end_date=s.end_date.date().isoformat() if s.end_date else None,
        days_of_week=s.days_of_week,
        priority=s.priority,
        loop_count=s.loop_count,
    )


def _sync_hash(sync: SyncMessage) -> str:
    """Compute a hash of a sync message for dedup."""
    return hashlib.md5(sync.model_dump_json().encode()).hexdigest()


async def build_device_sync(device_id: str, db) -> SyncMessage | None:
    """Build a full SyncMessage for a specific device.

    Used by both the scheduler loop and the on-change push.
    Returns None if the database isn't ready.
    """
    # Read configured timezone (per-device overrides CMS global)
    tz_result = await db.execute(
        select(CMSSetting.value).where(CMSSetting.key == "timezone")
    )
    cms_tz = tz_result.scalar_one_or_none() or "UTC"

    now = datetime.now(timezone.utc)
    cutoff = now + timedelta(days=SCHEDULE_WINDOW_DAYS)
    # Convert to local time for date comparisons — schedule dates represent
    # days in the CMS timezone, not UTC (e.g. a schedule ending April 1st
    # PDT is still valid at 9 PM PDT even though it's April 2nd UTC).
    local_now = now.astimezone(ZoneInfo(cms_tz))
    local_cutoff = cutoff.astimezone(ZoneInfo(cms_tz))

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
        # Use .date() on local time — schedule dates represent days in the CMS timezone
        today = local_now.date()
        cutoff_date = local_cutoff.date()
        if s.end_date:
            end_d = s.end_date.date() if hasattr(s.end_date, 'date') else s.end_date
            if end_d < today:
                continue
        if s.start_date:
            start_d = s.start_date.date() if hasattr(s.start_date, 'date') else s.start_date
            if start_d > cutoff_date:
                continue

        target_ids = await _get_target_device_ids(s, db)
        if device_id in target_ids:
            # Skip if this schedule's current occurrence is being skipped
            if str(s.id) in _skipped:
                continue
            entries.append(_schedule_to_entry(s, variant_checksums))

    # Per-device timezone overrides the CMS global timezone
    device_tz = dev.timezone or cms_tz

    return SyncMessage(
        device_status=dev.status.value if dev.status else None,
        timezone=device_tz,
        schedules=entries,
        default_asset=default_asset_name,
        default_asset_checksum=default_asset_checksum or None,
        splash=default_asset_name,
    )


async def push_sync_to_device(device_id: str, db) -> None:
    """Build and push a fresh sync to a single connected device."""
    if not device_manager.is_connected(device_id):
        return

    # Only sync adopted devices — pending/orphaned devices should not receive content
    result = await db.execute(select(Device.status).where(Device.id == device_id))
    status = result.scalar_one_or_none()
    if status != DeviceStatus.ADOPTED:
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
    """Single evaluation pass: sync schedules to devices and detect MISSED playback."""
    if not device_manager.connected_count:
        return

    if _db._session_factory is None:
        return

    now = datetime.now(timezone.utc)

    async with _db._session_factory() as db:
        # Read timezone for schedule evaluation
        tz_result = await db.execute(
            select(CMSSetting.value).where(CMSSetting.key == "timezone")
        )
        tz_name = tz_result.scalar_one_or_none() or "UTC"
        local_now = now.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)

        connected = set(device_manager.connected_ids)

        # Push full sync to each connected device (dedup via hash)
        for did in connected:
            await push_sync_to_device(did, db)

        # ── Detect MISSED schedules ──
        result = await db.execute(
            select(Schedule)
            .options(
                selectinload(Schedule.asset),
                selectinload(Schedule.group),
            )
            .where(Schedule.enabled == True)  # noqa: E712
        )
        schedules = result.scalars().all()

        # Purge expired skips
        expired = [sid for sid, until in _skipped.items() if local_now >= until]
        for sid in expired:
            _skipped.pop(sid, None)
            # Clear sync hash so the schedule gets re-pushed on next eval
            for did in connected:
                _last_sync_hash.pop(did, None)

        active = [
            s for s in schedules
            if _matches_now(s, local_now) and str(s.id) not in _skipped
        ]

        # Detect MISSED schedules: active schedules targeting offline adopted devices
        # Only log MISSED after MISSED_GRACE_SECONDS of continuous offline
        all_adopted_q = await db.execute(
            select(Device.id, Device.name).where(Device.status == DeviceStatus.ADOPTED)
        )
        all_adopted = {r[0]: (r[1] or r[0]) for r in all_adopted_q.all()}

        utc_now = datetime.now(timezone.utc)

        for s in active:
            if not s.asset:
                continue
            target_ids = await _get_target_device_ids(s, db)
            for did in target_ids:
                key = (str(s.id), did)
                if did in connected or did not in all_adopted:
                    # Device is online or not adopted — clear offline tracking
                    _offline_since.pop(key, None)
                    continue
                # Device is offline and adopted
                if key not in _offline_since:
                    _offline_since[key] = utc_now
                elapsed = (utc_now - _offline_since[key]).total_seconds()
                if elapsed >= MISSED_GRACE_SECONDS and key not in _missed_logged:
                    _missed_logged.add(key)
                    await _log_event(
                        db, ScheduleLogEvent.MISSED,
                        schedule_name=s.name,
                        device_name=all_adopted.get(did, did),
                        asset_filename=s.asset.filename,
                        schedule_id=s.id, device_id=did,
                        details=f"Device offline for {int(elapsed)}s",
                    )

        # Clear missed/offline tracking for combos that are no longer active
        active_keys = set()
        for s in active:
            if not s.asset:
                continue
            target_ids = await _get_target_device_ids(s, db)
            for did in target_ids:
                if did not in connected and did in all_adopted:
                    active_keys.add((str(s.id), did))
        _missed_logged.difference_update(_missed_logged - active_keys)
        # Clear offline_since for combos no longer relevant
        stale_offline = [k for k in _offline_since if k not in active_keys]
        for k in stale_offline:
            del _offline_since[k]

        # Clean up _now_playing for devices that disconnected
        stale = [did for did in list(_now_playing) if did not in connected]
        for did in stale:
            _now_playing.pop(did, None)

        # Clean up _now_playing entries whose schedule window has expired
        active_sids = {str(s.id) for s in active}
        expired_np = [
            did for did, info in list(_now_playing.items())
            if info.get("schedule_id") and str(info["schedule_id"]) not in active_sids
        ]
        for did in expired_np:
            info = _now_playing.pop(did, None)
            if info:
                logger.info(
                    "Cleared stale now_playing for device %s (schedule %s expired)",
                    did, info.get("schedule_name", "?"),
                )

        await db.commit()


async def scheduler_loop() -> None:
    """Background loop that periodically evaluates schedules."""
    logger.info("Scheduler started (interval=%ds)", EVAL_INTERVAL_SECONDS)
    while True:
        try:
            await evaluate_schedules()
        except Exception:
            logger.exception("Scheduler evaluation error")
        await asyncio.sleep(EVAL_INTERVAL_SECONDS)
