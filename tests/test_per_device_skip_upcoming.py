"""Regression: per-device "End Now" should hide a schedule from
"Coming Up" when every adopted target device has an active
per-device skip.

Bug: dashboard's End Now button always posts ``device_id``, so the
server writes a per-device ``ScheduleDeviceSkip`` row (not
``Schedule.skipped_until``).  Before the fix, ``get_upcoming_schedules``
only received schedule-wide skips, so a single-target schedule
re-appeared in Coming Up with a stuck "Starting…" badge.
"""

import uuid
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

from cms.models.asset import Asset, AssetType
from cms.models.schedule import Schedule
from cms.services.scheduler import get_upcoming_schedules


UTC = ZoneInfo("UTC")


def _make_schedule(
    start_time: time, end_time: time, *, group_id=None, name: str = "s",
) -> Schedule:
    asset = Asset(
        filename="video.mp4", asset_type=AssetType.VIDEO,
        size_bytes=1000, checksum="abc",
    )
    s = Schedule(
        name=name,
        asset_id=uuid.uuid4(),
        group_id=group_id,
        enabled=True,
        start_time=start_time,
        end_time=end_time,
        priority=0,
    )
    s.id = uuid.uuid4()
    s.asset = asset
    s.device = None
    s.group = None
    return s


def _noon_utc() -> datetime:
    # Noon today in UTC so the 08:00-17:00 window matches "now"
    return datetime.now(UTC).replace(hour=12, minute=0, second=0, microsecond=0)


class TestPerDeviceSkipHidesSchedule:
    """When ``per_device_skipped`` covers every ADOPTED target device
    of a schedule, it should be hidden from Coming Up even though the
    time window matches and there is no now_playing entry for it."""

    def test_single_target_hidden_when_per_device_skipped(self):
        gid = uuid.uuid4()
        did = "dev-A"
        s = _make_schedule(time(8, 0), time(17, 0), group_id=gid, name="solo")
        now = _noon_utc()
        # Empty now_playing (device stopped after End Now)
        result = get_upcoming_schedules(
            [s], now, UTC, now_playing=[],
            per_device_skipped={(str(s.id), did)},
            target_devices_by_schedule={str(s.id): {did}},
        )
        assert result == []

    def test_multi_target_all_skipped_hidden(self):
        gid = uuid.uuid4()
        s = _make_schedule(time(8, 0), time(17, 0), group_id=gid, name="multi")
        now = _noon_utc()
        targets = {"dev-A", "dev-B", "dev-C"}
        per_device = {(str(s.id), d) for d in targets}
        result = get_upcoming_schedules(
            [s], now, UTC, now_playing=[],
            per_device_skipped=per_device,
            target_devices_by_schedule={str(s.id): targets},
        )
        assert result == []

    def test_multi_target_partial_skip_not_hidden_by_per_device(self):
        """Only some target devices skipped: per-device filter does NOT
        hide the schedule.  (Other device would normally be playing and
        show as ``winning`` via now_playing, but this unit-level test
        just asserts the per-device branch doesn't over-filter.)
        """
        gid = uuid.uuid4()
        s = _make_schedule(time(8, 0), time(17, 0), group_id=gid, name="multi")
        now = _noon_utc()
        targets = {"dev-A", "dev-B"}
        # Only A skipped; B still available
        per_device = {(str(s.id), "dev-A")}
        result = get_upcoming_schedules(
            [s], now, UTC, now_playing=[],
            per_device_skipped=per_device,
            target_devices_by_schedule={str(s.id): targets},
        )
        # Not filtered by per-device; falls through to "starting" entry
        # (no now_playing winner yet) — this matches pre-fix behaviour
        # for the "some targets skipped, others transitioning" case.
        assert len(result) == 1
        assert result[0]["starting"] is True

    def test_no_per_device_info_preserves_legacy_behaviour(self):
        """When callers don't provide per-device info (the default),
        behaviour is unchanged — schedule shows as starting."""
        gid = uuid.uuid4()
        s = _make_schedule(time(8, 0), time(17, 0), group_id=gid, name="legacy")
        now = _noon_utc()
        result = get_upcoming_schedules(
            [s], now, UTC, now_playing=[],
        )
        assert len(result) == 1
        assert result[0]["starting"] is True

    def test_targets_only_pending_devices_does_not_hide(self):
        """If ADOPTED target set is empty (e.g. all devices pending),
        ``target_devices_by_schedule`` maps to an empty set — the
        per-device branch's ``all(...)`` on empty is True, but the
        helper returns an empty set only when group has no ADOPTED
        devices.  Guard: we only filter when ``targets`` is truthy."""
        gid = uuid.uuid4()
        s = _make_schedule(time(8, 0), time(17, 0), group_id=gid, name="empty-targets")
        now = _noon_utc()
        result = get_upcoming_schedules(
            [s], now, UTC, now_playing=[],
            per_device_skipped=set(),
            target_devices_by_schedule={str(s.id): set()},
        )
        # Empty targets → per-device branch skipped → legacy "starting"
        assert len(result) == 1
        assert result[0]["starting"] is True
