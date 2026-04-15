"""Tests for dashboard transition gap — issue #99.

Schedules should never vanish from the dashboard during the window between
entering their time slot and the scheduler background task updating
``_now_playing``.
"""

import uuid
from datetime import datetime, time, timezone
from zoneinfo import ZoneInfo

import pytest

from cms.models.asset import Asset, AssetType
from cms.models.schedule import Schedule
from cms.services.scheduler import (
    _skipped,
    get_upcoming_schedules,
)

UTC = ZoneInfo("UTC")


def _make_schedule(
    start_time: time,
    end_time: time,
    priority: int = 0,
    name: str = "test",
    asset_filename: str = "video.mp4",
    group_id=None,
    enabled: bool = True,
) -> Schedule:
    """Create a Schedule with an Asset attached for testing."""
    asset = Asset(
        filename=asset_filename, asset_type=AssetType.VIDEO,
        size_bytes=1000, checksum="abc",
    )
    s = Schedule(
        name=name,
        asset_id=uuid.uuid4(),
        group_id=group_id,
        enabled=enabled,
        start_time=start_time,
        end_time=end_time,
        priority=priority,
    )
    s.id = uuid.uuid4()
    s.asset = asset
    s.device = None
    s.group = None
    return s


def _now_playing_entry(schedule: Schedule, device_id: str) -> dict:
    return {
        "device_id": device_id,
        "schedule_id": str(schedule.id),
        "schedule_name": schedule.name,
        "asset_filename": schedule.asset.filename,
        "end_time": schedule.end_time.strftime("%I:%M %p").lstrip("0"),
        "since": datetime.now(timezone.utc).isoformat(),
    }


class TestTransitionGap:
    """Schedule must stay visible when it enters its time window but the
    scheduler hasn't updated ``_now_playing`` yet."""

    def setup_method(self):
        _skipped.clear()

    def teardown_method(self):
        _skipped.clear()

    def test_active_schedule_not_yet_in_now_playing_stays_visible(self):
        """A schedule that just entered its window (not yet in now_playing)
        should appear in upcoming with ``starting=True``."""
        gid = uuid.uuid4()
        s = _make_schedule(
            time(10, 0), time(11, 0), name="Morning Show", group_id=gid,
        )
        # It's 10:05 — schedule is active but scheduler hasn't processed it
        now = datetime(2026, 3, 28, 10, 5, tzinfo=timezone.utc)
        now_playing = []  # scheduler hasn't added it yet

        result = get_upcoming_schedules([s], now, UTC, now_playing=now_playing)
        assert len(result) == 1
        assert result[0]["schedule_name"] == "Morning Show"
        assert result[0].get("starting") is True

    def test_starting_entry_has_zero_countdown(self):
        """A starting entry should have starts_in_seconds=0 and an appropriate
        countdown string."""
        gid = uuid.uuid4()
        s = _make_schedule(
            time(10, 0), time(11, 0), name="Show", group_id=gid,
        )
        now = datetime(2026, 3, 28, 10, 5, tzinfo=timezone.utc)

        result = get_upcoming_schedules([s], now, UTC, now_playing=[])
        assert result[0]["starts_in_seconds"] == 0

    def test_starting_entry_disappears_once_in_now_playing(self):
        """Once the scheduler processes it (appears in now_playing), it should
        no longer appear in upcoming."""
        gid = uuid.uuid4()
        s = _make_schedule(
            time(10, 0), time(11, 0), name="Show", group_id=gid,
        )
        now = datetime(2026, 3, 28, 10, 5, tzinfo=timezone.utc)
        now_playing = [_now_playing_entry(s, "d1")]

        result = get_upcoming_schedules([s], now, UTC, now_playing=now_playing)
        assert len(result) == 0

    def test_skipped_schedule_not_shown_as_starting(self):
        """A schedule that was skipped/ended should NOT appear as starting."""
        gid = uuid.uuid4()
        s = _make_schedule(
            time(10, 0), time(11, 0), name="Ended", group_id=gid,
        )
        _skipped[str(s.id)] = datetime(2026, 3, 28, 11, 0)
        now = datetime(2026, 3, 28, 10, 5, tzinfo=timezone.utc)

        result = get_upcoming_schedules([s], now, UTC, now_playing=[])
        assert len(result) == 0


class TestPreemptionTransitionGap:
    """When a higher-priority schedule enters its window during an active
    lower-priority schedule, it should not vanish while waiting for the
    scheduler to process the switch."""

    def setup_method(self):
        _skipped.clear()

    def teardown_method(self):
        _skipped.clear()

    def test_new_higher_priority_schedule_shows_starting(self):
        """Higher-priority schedule just entered its window — should show as
        starting, not vanish."""
        gid = uuid.uuid4()
        low = _make_schedule(
            time(8, 0), time(12, 0), priority=1, name="Low", group_id=gid,
        )
        high = _make_schedule(
            time(10, 0), time(11, 0), priority=10, name="High", group_id=gid,
        )
        # 10:02 — both active, but scheduler still shows low as winner
        now = datetime(2026, 3, 28, 10, 2, tzinfo=timezone.utc)
        now_playing = [_now_playing_entry(low, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=now_playing)
        # High should be visible as starting (not vanished)
        starting = [r for r in result if r.get("starting")]
        assert len(starting) == 1
        assert starting[0]["schedule_name"] == "High"

    def test_preempted_schedule_still_shows_when_both_unprocessed(self):
        """When neither schedule is in now_playing yet, the higher-priority
        one shows as starting and the lower-priority one shows as preempted
        or starting."""
        gid = uuid.uuid4()
        low = _make_schedule(
            time(10, 0), time(12, 0), priority=1, name="Low", group_id=gid,
        )
        high = _make_schedule(
            time(10, 0), time(11, 0), priority=10, name="High", group_id=gid,
        )
        now = datetime(2026, 3, 28, 10, 2, tzinfo=timezone.utc)
        now_playing = []  # neither processed yet

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=now_playing)
        names = {r["schedule_name"] for r in result}
        # At minimum, the high-priority schedule must be visible
        assert "High" in names
        # Total: nothing should have vanished — both should be visible
        assert len(result) == 2

    def test_starting_entry_has_standard_fields(self):
        """Starting entries should include all standard upcoming fields."""
        gid = uuid.uuid4()
        s = _make_schedule(
            time(10, 0), time(11, 0), name="Show", group_id=gid,
        )
        now = datetime(2026, 3, 28, 10, 5, tzinfo=timezone.utc)

        result = get_upcoming_schedules([s], now, UTC, now_playing=[])
        assert len(result) == 1
        entry = result[0]
        assert "schedule_name" in entry
        assert "asset_filename" in entry
        assert "target_name" in entry
        assert "start_time" in entry
        assert "end_time" in entry
        assert "duration_mins" in entry
        assert "day_label" in entry
        assert entry["day_label"] == "today"


class TestGroupScheduleTransitionGap:
    """Group-targeted schedules must also show 'starting' during the
    transition gap — they have device_id=None."""

    def setup_method(self):
        _skipped.clear()

    def teardown_method(self):
        _skipped.clear()

    def test_group_schedule_not_yet_in_now_playing_stays_visible(self):
        """A group schedule entering its window should show as 'starting'."""
        group_id = uuid.uuid4()
        s = _make_schedule(
            time(10, 0), time(11, 0), name="Group Show",
            group_id=group_id,
        )
        now = datetime(2026, 3, 28, 10, 5, tzinfo=timezone.utc)
        now_playing = []

        result = get_upcoming_schedules([s], now, UTC, now_playing=now_playing)
        assert len(result) == 1
        assert result[0]["schedule_name"] == "Group Show"
        assert result[0].get("starting") is True

    def test_group_schedule_disappears_once_in_now_playing(self):
        """Once the scheduler processes a group schedule (appears in
        now_playing for its devices), it should leave upcoming."""
        group_id = uuid.uuid4()
        s = _make_schedule(
            time(10, 0), time(11, 0), name="Group Show",
            group_id=group_id,
        )
        now = datetime(2026, 3, 28, 10, 5, tzinfo=timezone.utc)
        # Scheduler expanded group to d1, d2 — both entries use same schedule_id
        now_playing = [
            _now_playing_entry(s, "d1"),
            _now_playing_entry(s, "d2"),
        ]

        result = get_upcoming_schedules([s], now, UTC, now_playing=now_playing)
        assert len(result) == 0
