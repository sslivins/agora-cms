"""Schedule evaluator — background task that syncs schedules to devices."""

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from shared.database import get_session_factory as _get_session_factory
from cms.models.asset import Asset, AssetVariant, VariantStatus
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.schedule_device_skip import ScheduleDeviceSkip
from cms.models.schedule_log import ScheduleLog, ScheduleLogEvent
from cms.models.schedule_missed_event import ScheduleMissedEvent
from cms.models.setting import CMSSetting
from cms.schemas.protocol import ScheduleEntry, SyncMessage
from cms.services.transport import get_transport

logger = logging.getLogger("agora.cms.scheduler")


def _asset_display_name(asset) -> str:
    """Return the best human-readable name for an asset."""
    if asset is None:
        return "—"
    return asset.display_name or asset.original_filename or asset.filename

# Track last sync hash per device to avoid re-sending identical syncs.
# NOTE: this is replica-local. Under N>1 replicas two replicas may both
# push to the same device, but the device firmware deduplicates via the
# sync_hash echoed in acks. See docs/multi-replica.md.
_last_sync_hash: dict[str, str] = {}

# Confirmed playback: minimal tracking of what each device has confirmed
# it's playing.  Populated by WS PLAYBACK_STARTED, cleared by PLAYBACK_ENDED
# or device disconnect.  Only stores {device_id: {schedule_id, since}}.
# NOTE: replica-local. Tracked separately in the confirmed-playing-dbback
# todo; every replica gets its own view until that lands.
_confirmed_playing: dict[str, dict] = {}
_now_playing = _confirmed_playing  # backwards-compat alias for tests


# ── Skip-state snapshot (DB-backed) ───────────────────────────────────
#
# Skip state (which schedules the operator has "ended now" for the rest
# of the day, schedule-wide or per-device) lives in two DB tables:
#   - Schedule.skipped_until          (schedule-wide)
#   - ScheduleDeviceSkip              (per-device)
#
# Historically this module kept an in-memory cache mirroring those rows,
# but the cache was written only by the replica that handled the skip
# API call. Under multi-replica deployments every other replica would
# happily push syncs containing the "skipped" schedule (and the scheduler
# loop on a different replica would evaluate as if no skip existed).
#
# The cache is now gone. Every top-level consumer (scheduler tick,
# build_device_sync, push_sync_to_device, compute_now_playing, UI
# routes calling get_upcoming_schedules) loads a fresh SkipSnapshot
# from the DB at the start of its pass and threads it through. The
# snapshot is cheap — two small indexed queries — and is consistent for
# the duration of a single request/tick.

@dataclass(frozen=True)
class SkipSnapshot:
    """Immutable view of currently-persisted schedule skips.

    ``schedule_wide`` maps ``schedule_id`` -> ``skip_until`` (naive local
    datetime, CMS timezone).  ``per_device`` maps
    ``(schedule_id, device_id)`` -> ``skip_until``.
    """

    schedule_wide: dict[str, datetime] = field(default_factory=dict)
    per_device: dict[tuple[str, str], datetime] = field(default_factory=dict)

    @classmethod
    def empty(cls) -> "SkipSnapshot":
        return cls()

    def active_as_of(self, local_now: datetime) -> "SkipSnapshot":
        """Return a new snapshot with expired entries dropped."""
        sw = {
            sid: until for sid, until in self.schedule_wide.items()
            if local_now < until
        }
        pd = {
            key: until for key, until in self.per_device.items()
            if local_now < until
        }
        return SkipSnapshot(schedule_wide=sw, per_device=pd)

    def expired_schedule_ids(self, local_now: datetime) -> list[str]:
        return [
            sid for sid, until in self.schedule_wide.items()
            if local_now >= until
        ]

    def expired_device_pairs(self, local_now: datetime) -> list[tuple[str, str]]:
        return [
            key for key, until in self.per_device.items()
            if local_now >= until
        ]

    def is_schedule_skipped(self, schedule_id: str) -> bool:
        """True iff a schedule-wide skip is active for this schedule."""
        return schedule_id in self.schedule_wide

    def is_skipped_for_device(self, schedule_id: str, device_id: str) -> bool:
        """True iff schedule is skipped for this device (wide OR per-device)."""
        if schedule_id in self.schedule_wide:
            return True
        return (schedule_id, device_id) in self.per_device


async def load_skip_snapshot(db) -> SkipSnapshot:
    """Load all persisted skips from DB into an immutable snapshot.

    Two small indexed queries.  Returns both expired and active entries;
    callers that only want active ones should call ``.active_as_of(now)``.
    """
    sw: dict[str, datetime] = {}
    sw_result = await db.execute(
        select(Schedule.id, Schedule.skipped_until)
        .where(Schedule.skipped_until.isnot(None))
    )
    for sid, until in sw_result.all():
        sw[str(sid)] = until.replace(tzinfo=None) if until.tzinfo else until

    pd: dict[tuple[str, str], datetime] = {}
    pd_result = await db.execute(
        select(
            ScheduleDeviceSkip.schedule_id,
            ScheduleDeviceSkip.device_id,
            ScheduleDeviceSkip.skip_until,
        )
    )
    for sid, did, until in pd_result.all():
        key = (str(sid), did)
        pd[key] = until.replace(tzinfo=None) if until.tzinfo else until

    return SkipSnapshot(schedule_wide=sw, per_device=pd)


# ── MISSED-event dedup (DB-backed, N>1 failover safe) ───────────────
#
# The scheduler's MISSED-alert dedup + grace-clock state used to live
# in the ``_missed_logged`` and ``_offline_since`` module-level dicts.
# Those are replica-local and on leader failover (deploy rollover,
# pod crash) the new leader would start with empty memory, which both
# (a) restarts the grace clock from zero, delaying MISSED emission, and
# (b) re-emits MISSED for schedule+device combos the prior leader
# already alerted on.  State now lives in ``schedule_missed_events``
# keyed by (schedule_id, device_id, occurrence_date).

def _insert_missed_event_stmt(schedule_id, device_id, occurrence_date, first_seen):
    """Return a dialect-aware ``INSERT ... ON CONFLICT DO NOTHING`` stmt.

    Ensures the first observation of ``(schedule, device, date)`` seeds
    ``first_seen_offline_at`` without clobbering an existing grace-clock
    entry written by a prior replica or tick.
    """
    from cms.database import get_engine
    engine = get_engine()
    dialect = engine.dialect.name if engine is not None else "sqlite"
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        return pg_insert(ScheduleMissedEvent).values(
            schedule_id=schedule_id,
            device_id=device_id,
            occurrence_date=occurrence_date,
            first_seen_offline_at=first_seen,
        ).on_conflict_do_nothing(
            index_elements=["schedule_id", "device_id", "occurrence_date"],
        )
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    return sqlite_insert(ScheduleMissedEvent).values(
        schedule_id=schedule_id,
        device_id=device_id,
        occurrence_date=occurrence_date,
        first_seen_offline_at=first_seen,
    ).on_conflict_do_nothing(
        index_elements=["schedule_id", "device_id", "occurrence_date"],
    )


EVAL_INTERVAL_SECONDS = 15
MISSED_GRACE_SECONDS = 60
SCHEDULE_WINDOW_DAYS = 30
MISSED_RETENTION_DAYS = 7  # prune dedup rows older than this each tick


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
    """Return a list of confirmed playback entries (minimal).

    For the full dashboard view with schedule details, use
    ``compute_now_playing()`` instead.  This is kept for callers that
    only need to know *which devices* are currently scheduled.
    """
    return [d.copy() for d in _confirmed_playing.values()]


def set_now_playing(device_id: str, entry: dict) -> None:
    """Record that a device confirmed playback (called by WS handler on PLAYBACK_STARTED)."""
    _confirmed_playing[device_id] = entry


def clear_now_playing(device_id: str) -> dict | None:
    """Remove confirmed playback for a device (called by WS handler on PLAYBACK_ENDED)."""
    return _confirmed_playing.pop(device_id, None)


def skip_schedule_until(
    schedule_id: str,
    until: datetime | None = None,
    device_id: str | None = None,
) -> None:
    """Invalidate confirmed-playing cache after a skip has been written to DB.

    **NOTE:** as of the scheduler-state-dbback refactor, this function no
    longer stores skip state.  Skip state is persisted in
    ``Schedule.skipped_until`` / ``ScheduleDeviceSkip`` by the API router.
    All read paths load a fresh :class:`SkipSnapshot` from DB and don't
    consult any module-level cache.

    The function is kept (with its original signature) because the router
    still needs a hook to invalidate the process-local
    ``_confirmed_playing`` dict so the dashboard reflects the change on
    this replica immediately.  The ``until`` parameter is ignored.
    """
    _ = until  # signature-compat only; skip state is persisted in DB
    if device_id is None:
        to_remove = [
            did for did, info in _confirmed_playing.items()
            if info.get("schedule_id") == schedule_id
        ]
        for did in to_remove:
            _confirmed_playing.pop(did, None)
        return

    info = _confirmed_playing.get(device_id)
    if info and info.get("schedule_id") == schedule_id:
        _confirmed_playing.pop(device_id, None)


def clear_schedule_skip(schedule_id: str, device_id: str | None = None) -> None:
    """No-op kept for router backward-compat.

    Prior to scheduler-state-dbback this cleared the module-level skip
    dicts so a freshly-enabled schedule would re-evaluate.  Now that skip
    state lives only in the DB (and expired rows are purged by the
    scheduler tick), the API router's DB writes are authoritative and
    no in-memory cleanup is needed.
    """
    _ = schedule_id, device_id  # signature-compat only


def clear_sync_hash(device_id: str) -> None:
    """Clear the cached sync hash for a device so the next eval re-sends."""
    _last_sync_hash.pop(device_id, None)


async def compute_now_playing(db, tz: ZoneInfo, now: datetime) -> list[dict]:
    """Compute the "currently playing" list from the DB + live device state.

    This is the **single source of truth** for dashboard rendering.  It queries
    active schedules from the DB, resolves target devices, and enriches each
    entry with live device state and confirmed-playback info.

    Returns a list of dicts ready for the dashboard template / JSON API.
    """
    skips = (await load_skip_snapshot(db)).active_as_of(
        now.astimezone(tz).replace(tzinfo=None)
    )
    from shared.models.asset import AssetType

    local_now = now.astimezone(tz).replace(tzinfo=None)

    # Get all enabled schedules with their assets and groups
    result = await db.execute(
        select(Schedule)
        .options(
            selectinload(Schedule.asset),
            selectinload(Schedule.group),
        )
        .where(Schedule.enabled == True)  # noqa: E712
    )
    schedules = result.scalars().all()

    # Filter to currently active schedules (in their time window, not skipped)
    active = [
        s for s in schedules
        if _matches_now(s, local_now) and not skips.is_schedule_skipped(str(s.id))
    ]

    if not active:
        return []

    # Resolve target devices for each active schedule (filter per-device skips)
    now_playing = []
    live_states = {s["device_id"]: s for s in await get_transport().get_all_states()}

    # Get device names
    all_device_ids = set()
    schedule_targets: list[tuple] = []  # (schedule, [device_ids])
    for s in active:
        if not s.asset:
            continue
        target_ids = await _get_target_device_ids(s, db)
        # Drop any device with an active per-device skip on this schedule
        target_ids = [
            did for did in target_ids
            if not skips.is_skipped_for_device(str(s.id), did)
        ]
        if not target_ids:
            continue
        schedule_targets.append((s, target_ids))
        all_device_ids.update(target_ids)

    # Fetch device names in one query
    if all_device_ids:
        name_q = await db.execute(
            select(Device.id, Device.name).where(Device.id.in_(all_device_ids))
        )
        device_names = {r[0]: (r[1] or r[0]) for r in name_q.all()}
    else:
        device_names = {}

    # Build per-device entries, picking highest-priority schedule per device
    device_schedule: dict[str, tuple] = {}  # device_id -> (priority, schedule)
    for s, target_ids in schedule_targets:
        for did in target_ids:
            existing = device_schedule.get(did)
            if existing is None or s.priority > existing[0]:
                device_schedule[did] = (s.priority, s)

    for did, (_, s) in device_schedule.items():
        is_saved_stream = s.asset.asset_type == AssetType.SAVED_STREAM
        is_url_asset = (
            s.asset.asset_type in (AssetType.WEBPAGE, AssetType.STREAM)
        )
        asset_raw = s.asset.url if is_url_asset else s.asset.filename
        display_name = _asset_display_name(s.asset)

        device_name = device_names.get(did, did)
        confirmed = _confirmed_playing.get(did)
        # Use confirmed "since" if this device confirmed THIS schedule
        since = now.isoformat()
        if confirmed and confirmed.get("schedule_id") == str(s.id):
            since = confirmed.get("since", since)

        entry = {
            "device_id": did,
            "device_name": device_name,
            "schedule_id": str(s.id),
            "schedule_name": s.name,
            "asset_filename": display_name,
            "asset_raw": asset_raw,
            "since": since,
            "source": "confirmed" if (confirmed and confirmed.get("schedule_id") == str(s.id)) else "scheduled",
            "end_time": s.end_time.strftime("%I:%M %p").lstrip("0"),
            "start_time_raw": s.start_time.strftime("%H:%M:%S"),
            "end_time_raw": s.end_time.strftime("%H:%M:%S"),
        }
        now_playing.append(entry)

    return now_playing


def get_upcoming_schedules(
    schedules: list, now: datetime, tz: ZoneInfo,
    now_playing: list[dict] | None = None,
    offline_device_ids: set[str] | None = None,
    skipped_schedule_ids: set[str] | None = None,
) -> list[dict]:
    """Return schedules starting within the next 24 hours.

    Schedules currently in their time window but preempted by a higher-priority
    schedule on the same target are included with ``preempted=True`` and a
    ``resumes_at`` hint when possible.

    Each entry includes start/end time, duration, countdown, and whether it's
    today or tomorrow.

    ``skipped_schedule_ids`` is the set of schedule IDs (as strings) that the
    operator has "Ended Now" and should therefore be hidden from the upcoming
    list.  Callers should pass ``skips.schedule_wide.keys()`` from a
    :func:`load_skip_snapshot` (filtered to active entries) — the default of
    ``None`` treats no schedules as skipped, which is safe for unit tests
    that never exercise the skip path.
    """
    _offline = offline_device_ids or set()
    _skipped_ids: set[str] = set(skipped_schedule_ids or ())
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
            if str(s.id) in _skipped_ids:
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
        "asset_filename": _asset_display_name(s.asset),
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
        "asset_filename": _asset_display_name(s.asset),
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
        "asset_filename": _asset_display_name(s.asset),
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

    # SAVED_STREAM assets behave like normal videos (file download)
    # STREAM assets are URL-based (direct stream playback)
    is_saved_stream = (
        s.asset
        and s.asset.asset_type == AssetType.SAVED_STREAM
    )
    is_url_asset = (
        s.asset
        and s.asset.asset_type in (AssetType.WEBPAGE, AssetType.STREAM)
    )
    return ScheduleEntry(
        id=str(s.id),
        name=s.name,
        asset=s.asset.filename,
        asset_checksum=None if is_url_asset else checksum,
        asset_type="video" if is_saved_stream else (s.asset.asset_type.value if s.asset else None),
        url=s.asset.url if is_url_asset else None,
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


async def build_device_sync(
    device_id: str,
    db,
    skips: "SkipSnapshot | None" = None,
) -> SyncMessage | None:
    """Build a full SyncMessage for a specific device.

    Used by both the scheduler loop and the on-change push.
    Returns None if the database isn't ready.

    ``skips`` is an optional pre-loaded snapshot of active schedule skips.
    If omitted, this function loads and filters one itself.  The scheduler
    tick passes its own snapshot in to amortize the cost across every
    device it syncs.
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

    if skips is None:
        skips = (await load_skip_snapshot(db)).active_as_of(
            local_now.replace(tzinfo=None)
        )

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
    # With the variant-swap model, multiple READY variants can transiently
    # exist for the same (asset, profile) while a newer one is being
    # promoted.  Order by created_at DESC so the FIRST write per asset
    # filename is the latest READY variant — subsequent (older) rows are
    # skipped by the `not in` guard below.
    variant_checksums: dict[str, str] = {}
    if dev.profile_id:
        var_result = await db.execute(
            select(AssetVariant)
            .options(selectinload(AssetVariant.source_asset))
            .where(
                AssetVariant.profile_id == dev.profile_id,
                AssetVariant.status == VariantStatus.READY,
                AssetVariant.deleted_at.is_(None),
            )
            .order_by(AssetVariant.created_at.desc())
        )
        for v in var_result.scalars().all():
            fname = v.source_asset.filename
            if fname in variant_checksums:
                continue  # already have the latest READY for this asset
            variant_checksums[fname] = v.checksum
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
            # (either schedule-wide, or just for this device).
            if skips.is_skipped_for_device(str(s.id), device_id):
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


async def push_sync_to_device(
    device_id: str,
    db,
    skips: "SkipSnapshot | None" = None,
) -> None:
    """Build and push a fresh sync to a single connected device.

    ``skips`` may be an already-loaded :class:`SkipSnapshot` (active
    entries only) so the scheduler tick can share one snapshot across
    every device it syncs.  If omitted, :func:`build_device_sync` loads
    its own.
    """
    if not await get_transport().is_connected(device_id):
        return

    # Only sync adopted devices — pending/orphaned devices should not receive content
    result = await db.execute(select(Device.status).where(Device.id == device_id))
    status = result.scalar_one_or_none()
    if status != DeviceStatus.ADOPTED:
        return

    sync = await build_device_sync(device_id, db, skips=skips)
    if sync is None:
        return

    h = _sync_hash(sync)
    if _last_sync_hash.get(device_id) == h:
        return

    await get_transport().send_to_device(device_id, sync.model_dump(mode="json"))
    _last_sync_hash[device_id] = h
    logger.info("Pushed full sync to device %s (%d schedules)", device_id, len(sync.schedules))


async def push_sync_to_affected_devices(schedule: Schedule, db) -> None:
    """Push sync to all devices affected by a schedule change."""
    target_ids = await _get_target_device_ids(schedule, db)
    for did in target_ids:
        await push_sync_to_device(did, db)


async def evaluate_schedules() -> None:
    """Single evaluation pass: sync schedules to devices and detect MISSED playback.

    Runs on every tick regardless of connected-device count — the MISSED
    block specifically needs to fire when devices are offline, which is
    when ``connected_count() == 0`` is most likely.  The sync-push loop
    below no-ops when ``connected`` is empty.
    """
    sf = _get_session_factory()
    if sf is None:
        return

    now = datetime.now(timezone.utc)

    async with sf() as db:
        # Read timezone for schedule evaluation
        tz_result = await db.execute(
            select(CMSSetting.value).where(CMSSetting.key == "timezone")
        )
        tz_name = tz_result.scalar_one_or_none() or "UTC"
        local_now = now.astimezone(ZoneInfo(tz_name)).replace(tzinfo=None)

        # Load the full skip snapshot once for this tick.  We use the
        # raw snapshot to compute expirations, then derive an
        # "active-as-of-now" view for downstream filtering.
        raw_skips = await load_skip_snapshot(db)
        skips = raw_skips.active_as_of(local_now)

        connected = set(await get_transport().connected_ids())

        # Push full sync to each connected device (dedup via hash).
        # Share the snapshot across all devices to amortize the DB load.
        for did in connected:
            await push_sync_to_device(did, db, skips=skips)

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

        # Purge expired schedule-wide skips from DB.
        expired = raw_skips.expired_schedule_ids(local_now)
        for sid in expired:
            await db.execute(
                Schedule.__table__.update()
                .where(Schedule.id == sid)
                .values(skipped_until=None)
            )
            # Clear sync hash so the schedule gets re-pushed on next eval
            for did in connected:
                _last_sync_hash.pop(did, None)
        if expired:
            await db.commit()

        # Purge expired per-device skips from DB.
        dev_expired = raw_skips.expired_device_pairs(local_now)
        for key in dev_expired:
            sid, did = key
            await db.execute(
                ScheduleDeviceSkip.__table__.delete().where(
                    (ScheduleDeviceSkip.schedule_id == sid)
                    & (ScheduleDeviceSkip.device_id == did)
                )
            )
            _last_sync_hash.pop(did, None)
        if dev_expired:
            await db.commit()

        active = [
            s for s in schedules
            if _matches_now(s, local_now) and not skips.is_schedule_skipped(str(s.id))
        ]

        # ── Detect MISSED schedules (DB-backed dedup + grace clock) ──
        #
        # For each (active schedule × offline adopted device) pair:
        #   1. Upsert a ``schedule_missed_events`` row keyed by
        #      (schedule_id, device_id, occurrence_date).  ON CONFLICT
        #      DO NOTHING preserves the original ``first_seen_offline_at``
        #      so the grace clock survives leader failover and sampling
        #      skew between replicas.
        #   2. Once elapsed >= grace, atomically claim emission via
        #      ``UPDATE ... WHERE emitted_at IS NULL RETURNING`` — only
        #      one replica can win for a given (schedule, device, date).
        #   3. Commit the claim *before* writing the ScheduleLog entry,
        #      so a downstream ``_log_event`` rollback (FK fallback on
        #      deleted schedule/device) cannot resurrect the claim and
        #      cause a duplicate MISSED emission on the next tick.
        all_adopted_q = await db.execute(
            select(Device.id, Device.name).where(Device.status == DeviceStatus.ADOPTED)
        )
        all_adopted = {r[0]: (r[1] or r[0]) for r in all_adopted_q.all()}

        utc_now = datetime.now(timezone.utc)
        occurrence_date = local_now.date()

        # (active_offline_keys) drives both emission and cleanup below.
        # ``active_offline_context`` preserves the schedule/device names
        # and asset filename we need for the MISSED log entry.
        active_offline_keys: set[tuple[str, str]] = set()
        active_offline_context: dict[tuple[str, str], dict] = {}

        for s in active:
            if not s.asset:
                continue
            target_ids = await _get_target_device_ids(s, db)
            for did in target_ids:
                # Don't flag MISSED for a device whose skip is active.
                if skips.is_skipped_for_device(str(s.id), did):
                    continue
                if did in connected or did not in all_adopted:
                    # Device is online or not adopted.
                    continue
                key = (str(s.id), did)
                active_offline_keys.add(key)
                active_offline_context[key] = {
                    "schedule_id": s.id,
                    "_device_id": did,
                    "schedule_name": s.name,
                    "device_name": all_adopted.get(did, did),
                    "asset_filename": _asset_display_name(s.asset),
                }

        # Step 1: seed the dedup row (no-op if already present).
        for (_sid_str, did), ctx in active_offline_context.items():
            await db.execute(
                _insert_missed_event_stmt(
                    ctx["schedule_id"], did, occurrence_date, utc_now,
                )
            )
        if active_offline_context:
            await db.commit()

        # Step 2: for each active×offline pair whose grace window has
        # elapsed, claim emission + write the MISSED log atomically in a
        # single outer transaction.  The ScheduleLog INSERT runs inside a
        # SAVEPOINT so a FK violation (schedule/device deleted mid-tick)
        # can be retried with null FKs without discarding the CAS claim.
        # If the savepoint AND its fallback both fail, we revert the CAS
        # claim (set ``emitted_at`` back to NULL) so the next tick retries
        # rather than silently dropping the MISSED.
        from sqlalchemy.exc import IntegrityError

        grace_cutoff = utc_now - timedelta(seconds=MISSED_GRACE_SECONDS)
        for (_sid_str, did), ctx in active_offline_context.items():
            # CAS claim.
            claim_result = await db.execute(
                ScheduleMissedEvent.__table__.update()
                .where(
                    (ScheduleMissedEvent.schedule_id == ctx["schedule_id"])
                    & (ScheduleMissedEvent.device_id == did)
                    & (ScheduleMissedEvent.occurrence_date == occurrence_date)
                    & (ScheduleMissedEvent.emitted_at.is_(None))
                    & (ScheduleMissedEvent.first_seen_offline_at <= grace_cutoff)
                )
                .values(emitted_at=utc_now)
                .returning(ScheduleMissedEvent.first_seen_offline_at)
            )
            row = claim_result.first()
            if row is None:
                continue

            first_seen = row[0]
            # SQLite strips timezone — coerce naive timestamps to UTC.
            if first_seen.tzinfo is None:
                first_seen = first_seen.replace(tzinfo=timezone.utc)
            elapsed = int((utc_now - first_seen).total_seconds())

            # Write ScheduleLog inside a nested SAVEPOINT so a FK failure
            # (schedule or device deleted mid-tick) can be caught and
            # retried without rolling back the CAS claim above.  Catch
            # ANY exception — high-temperature / missed-schedule alerts
            # must never be silently dropped.
            log_written = False
            _sched_id = ctx["schedule_id"] if isinstance(ctx["schedule_id"], uuid.UUID) else None
            if not isinstance(ctx["schedule_id"], uuid.UUID):
                try:
                    _sched_id = uuid.UUID(str(ctx["schedule_id"]))
                except (ValueError, TypeError):
                    _sched_id = None
            try:
                async with db.begin_nested():
                    db.add(ScheduleLog(
                        schedule_id=_sched_id,
                        schedule_name=ctx["schedule_name"],
                        device_id=did,
                        device_name=ctx["device_name"],
                        asset_filename=ctx["asset_filename"],
                        event=ScheduleLogEvent.MISSED,
                        details=f"Device offline for {elapsed}s",
                    ))
                log_written = True
            except IntegrityError:
                logger.warning(
                    "ScheduleLog FK violation for MISSED "
                    "(schedule=%s device=%s) — retrying without FKs",
                    _sched_id, did,
                )
                try:
                    async with db.begin_nested():
                        db.add(ScheduleLog(
                            schedule_id=None,
                            schedule_name=ctx["schedule_name"],
                            device_id=None,
                            device_name=ctx["device_name"],
                            asset_filename=ctx["asset_filename"],
                            event=ScheduleLogEvent.MISSED,
                            details=f"Device offline for {elapsed}s",
                        ))
                    log_written = True
                except Exception:
                    logger.exception(
                        "ScheduleLog null-FK retry failed for MISSED "
                        "(schedule=%s device=%s)",
                        _sched_id, did,
                    )
            except Exception:
                logger.exception(
                    "ScheduleLog insert failed for MISSED "
                    "(schedule=%s device=%s) — claim will be reverted "
                    "so next tick retries",
                    _sched_id, did,
                )

            if not log_written:
                # Revert the CAS claim so the next tick retries rather
                # than silently dropping the MISSED alert.
                await db.execute(
                    ScheduleMissedEvent.__table__.update()
                    .where(
                        (ScheduleMissedEvent.schedule_id == ctx["schedule_id"])
                        & (ScheduleMissedEvent.device_id == did)
                        & (ScheduleMissedEvent.occurrence_date == occurrence_date)
                        & (ScheduleMissedEvent.emitted_at == utc_now)
                    )
                    .values(emitted_at=None)
                )

        # Commit the claim + log (or revert) as a single atomic unit per
        # tick.  If this commit itself fails the whole batch rolls back
        # and the next tick will retry every claim it attempted here.
        await db.commit()

        # Cleanup: drop dedup rows whose (schedule, device) combo is no
        # longer in the active-offline set for today (device reconnected
        # or schedule deactivated) so a subsequent offline stretch gets
        # a fresh grace clock.  Also prune rows older than the retention
        # window so the table doesn't grow unbounded.
        stale_today_q = await db.execute(
            select(
                ScheduleMissedEvent.schedule_id, ScheduleMissedEvent.device_id,
            ).where(ScheduleMissedEvent.occurrence_date == occurrence_date)
        )
        stale_today = [
            (sid, did) for sid, did in stale_today_q.all()
            if (str(sid), did) not in active_offline_keys
        ]
        for sid, did in stale_today:
            await db.execute(
                ScheduleMissedEvent.__table__.delete().where(
                    (ScheduleMissedEvent.schedule_id == sid)
                    & (ScheduleMissedEvent.device_id == did)
                    & (ScheduleMissedEvent.occurrence_date == occurrence_date)
                )
            )
        retention_cutoff = occurrence_date - timedelta(days=MISSED_RETENTION_DAYS)
        await db.execute(
            ScheduleMissedEvent.__table__.delete().where(
                ScheduleMissedEvent.occurrence_date < retention_cutoff
            )
        )
        # Always commit here so cleanup is visible to the next tick even
        # when there were no emission claims above.
        await db.commit()

        # Clean up _confirmed_playing for devices that disconnected
        stale = [did for did in list(_confirmed_playing) if did not in connected]
        for did in stale:
            _confirmed_playing.pop(did, None)

        # Clean up _confirmed_playing entries whose schedule window has expired
        active_sids = {str(s.id) for s in active}
        expired_cp = [
            did for did, info in list(_confirmed_playing.items())
            if info.get("schedule_id") and str(info["schedule_id"]) not in active_sids
        ]
        for did in expired_cp:
            _confirmed_playing.pop(did, None)

        # Seed _confirmed_playing from live device state after CMS restart
        live_states = {
            s["device_id"]: s for s in await get_transport().get_all_states()
        }
        from shared.models.asset import AssetType
        for s in active:
            if not s.asset:
                continue
            target_ids = await _get_target_device_ids(s, db)
            for did in target_ids:
                if did in _confirmed_playing:
                    continue
                live = live_states.get(did)
                if not live or live.get("mode") != "play":
                    continue
                is_webpage = s.asset.asset_type == AssetType.WEBPAGE
                is_saved_stream = s.asset.asset_type == AssetType.SAVED_STREAM
                is_url_asset = (
                    s.asset.asset_type in (AssetType.WEBPAGE, AssetType.STREAM)
                )
                expected_raw = s.asset.url if is_url_asset else s.asset.filename
                if live.get("asset") != expected_raw:
                    continue
                # Device is playing this schedule's asset — seed confirmed
                _confirmed_playing[did] = {
                    "schedule_id": str(s.id),
                    "since": utc_now.isoformat(),
                }
                logger.info(
                    "Seeded confirmed_playing for device %s from live state "
                    "(schedule %s)",
                    did, s.name,
                )

        await db.commit()


async def scheduler_loop() -> None:
    """Background loop that periodically evaluates schedules.

    Stage 4 (#344): gated by a :class:`LeaderLease` so at most one
    replica dispatches syncs at a time.  Non-leaders sleep on the
    heartbeat interval and skip evaluation; on failover the new
    leader picks up within ``ttl_s`` and resumes ticks.  On SQLite
    (unit tests) the lease degrades to "always leader".
    """
    from cms.services.leader import LeaderLease

    logger.info("Scheduler started (interval=%ds)", EVAL_INTERVAL_SECONDS)
    lease = LeaderLease("scheduler", ttl_s=30, heartbeat_s=10)
    try:
        await lease.start()
        while True:
            try:
                if lease.is_leader:
                    await evaluate_schedules()
                else:
                    logger.debug("scheduler_loop: not leader, skipping tick")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Scheduler evaluation error")
            try:
                await asyncio.sleep(EVAL_INTERVAL_SECONDS)
            except asyncio.CancelledError:
                raise
    finally:
        await lease.stop()
