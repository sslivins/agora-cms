"""Tests for preempted schedule visibility in Coming Up."""

import uuid
from datetime import datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from cms.models.asset import Asset, AssetType
from cms.models.schedule import Schedule
from cms.services.scheduler import (
    _find_resume_time,
    _matches_now,
    _skipped,
    get_upcoming_schedules,
)


# ── Helpers ──


def _make_schedule(
    start_time: time,
    end_time: time,
    priority: int = 0,
    name: str = "test",
    asset_filename: str = "video.mp4",
    device_id: str | None = None,
    group_id=None,
    enabled: bool = True,
    days_of_week=None,
    start_date=None,
    end_date=None,
) -> Schedule:
    """Create a Schedule with an Asset attached for testing."""
    asset = Asset(
        filename=asset_filename, asset_type=AssetType.VIDEO,
        size_bytes=1000, checksum="abc",
    )
    s = Schedule(
        name=name,
        asset_id=uuid.uuid4(),
        device_id=device_id,
        group_id=group_id,
        enabled=enabled,
        start_time=start_time,
        end_time=end_time,
        start_date=start_date,
        end_date=end_date,
        days_of_week=days_of_week,
        priority=priority,
    )
    s.id = uuid.uuid4()
    s.asset = asset
    s.device = None
    s.group = None
    return s


def _now_playing_entry(schedule: Schedule, device_id: str) -> dict:
    """Build a now_playing entry as returned by get_now_playing()."""
    return {
        "device_id": device_id,
        "schedule_id": str(schedule.id),
        "schedule_name": schedule.name,
        "asset_filename": schedule.asset.filename,
        "end_time": schedule.end_time.strftime("%I:%M %p").lstrip("0"),
        "since": datetime.now(timezone.utc).isoformat(),
    }


UTC = ZoneInfo("UTC")


# ── Basic preemption visibility ──


class TestPreemptedInUpcoming:
    """Preempted schedules appear in Coming Up with resume info."""

    def test_preempted_schedule_appears_in_upcoming(self):
        """Low-priority schedule preempted by high-priority shows in Coming Up."""
        low = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 15), time(8, 20), priority=10, name="High",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 8, 16, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        assert len(result) == 1
        assert result[0]["schedule_name"] == "Low"
        assert result[0]["preempted"] is True

    def test_preempted_schedule_has_resumes_at(self):
        """Preempted entry includes resumes_at time."""
        low = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 15), time(8, 20), priority=10, name="High",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 8, 16, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        assert result[0]["resumes_at"] == "8:20 AM"

    def test_preempted_countdown_shows_resume_time(self):
        """Countdown says 'resumes in ...' instead of normal countdown."""
        low = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(8, 30), priority=10, name="High",
            device_id="d1",
        )
        # At 8:05, high playing, low preempted, resumes in 25 min
        now = datetime(2026, 3, 28, 8, 5, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        assert "resumes in 25 minutes" == result[0]["countdown"]

    def test_winning_schedule_excluded_from_upcoming(self):
        """The currently-winning schedule does not appear in Coming Up."""
        low = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 15), time(8, 20), priority=10, name="High",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 8, 16, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        names = [r["schedule_name"] for r in result]
        assert "High" not in names

    def test_preempted_entry_day_label_is_today(self):
        """Preempted entries are always labeled as 'today'."""
        low = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(8, 30), priority=10, name="High",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 8, 10, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        assert result[0]["day_label"] == "today"


# ── No now_playing (backward compatibility) ──


class TestLegacyBehavior:
    """When now_playing is None, active schedules are excluded as before."""

    def test_active_excluded_without_now_playing(self):
        """Without now_playing, active schedules are simply excluded."""
        s = _make_schedule(time(9, 0), time(17, 0), name="Active", device_id="d1")
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)

        result = get_upcoming_schedules([s], now, UTC)
        assert len(result) == 0

    def test_active_excluded_with_none_now_playing(self):
        """Explicitly passing None acts the same as omitting."""
        s = _make_schedule(time(9, 0), time(17, 0), name="Active", device_id="d1")
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)

        result = get_upcoming_schedules([s], now, UTC, now_playing=None)
        assert len(result) == 0


# ── Skipped schedules (End Now) not shown as preempted ──


class TestSkippedNotPreempted:
    """Schedules ended via End Now should not appear as preempted."""

    def setup_method(self):
        _skipped.clear()

    def teardown_method(self):
        _skipped.clear()

    def test_skipped_schedule_hidden(self):
        """A schedule that was 'Ended Now' should not appear in upcoming."""
        low = _make_schedule(
            time(8, 0), time(17, 0), priority=1, name="Ended",
            device_id="d1",
        )
        _skipped[str(low.id)] = datetime(2026, 3, 28, 17, 0)

        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        np = []  # no winner

        result = get_upcoming_schedules([low], now, UTC, now_playing=np)
        assert len(result) == 0

    def test_skipped_while_preempted_hidden(self):
        """A skipped schedule doesn't show as preempted even if a higher-priority
        schedule would normally preempt it."""
        low = _make_schedule(
            time(8, 0), time(17, 0), priority=1, name="Skipped Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(17, 0), priority=10, name="High",
            device_id="d1",
        )
        _skipped[str(low.id)] = datetime(2026, 3, 28, 17, 0)

        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        assert len(result) == 0


# ── Device offline (not preempted) ──


class TestDeviceOffline:
    """Schedules on offline devices should not appear as preempted."""

    def test_no_winner_no_preemption(self):
        """If no device is connected (empty now_playing), active schedules
        show as starting (scheduler hasn't processed them yet)."""
        s = _make_schedule(
            time(8, 0), time(17, 0), priority=1, name="Offline",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)

        result = get_upcoming_schedules([s], now, UTC, now_playing=[])
        assert len(result) == 1
        assert result[0]["starting"] is True

    def test_sole_schedule_on_connected_device_not_preempted(self):
        """A single active schedule that IS the winner should not be in upcoming."""
        s = _make_schedule(
            time(8, 0), time(17, 0), priority=1, name="Winner",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 12, 0, tzinfo=timezone.utc)
        np = [_now_playing_entry(s, "d1")]

        result = get_upcoming_schedules([s], now, UTC, now_playing=np)
        assert len(result) == 0


# ── Same-priority conflict ──


class TestSamePriorityConflict:
    """Two same-priority schedules on the same device: loser not shown as preempted."""

    def test_same_priority_loser_hidden(self):
        """Same-priority loser doesn't appear as preempted (no higher-priority preemptor)."""
        a = _make_schedule(
            time(8, 0), time(9, 0), priority=5, name="A",
            device_id="d1",
        )
        b = _make_schedule(
            time(8, 0), time(9, 0), priority=5, name="B",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 8, 30, tzinfo=timezone.utc)
        # B won arbitrarily
        np = [_now_playing_entry(b, "d1")]

        result = get_upcoming_schedules([a, b], now, UTC, now_playing=np)
        assert len(result) == 0


# ── Multiple preempting schedules (chaining) ──


class TestChainPreemption:
    """Multiple higher-priority schedules overlap the same low-priority one."""

    def test_resume_at_latest_preemptor(self):
        """When two preempting schedules overlap, resumes_at uses the later end time."""
        low = _make_schedule(
            time(8, 0), time(10, 0), priority=1, name="Low",
            device_id="d1",
        )
        mid = _make_schedule(
            time(8, 15), time(8, 30), priority=5, name="Mid",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 10), time(9, 0), priority=10, name="High",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 8, 20, tzinfo=timezone.utc)
        # High wins
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, mid, high], now, UTC, now_playing=np)
        preempted = [r for r in result if r.get("preempted")]
        assert len(preempted) >= 1
        low_entry = next(r for r in preempted if r["schedule_name"] == "Low")
        # Highest-priority later end = 9:00 (both Mid@8:30 and High@9:00 preempt, max is 9:00)
        assert low_entry["resumes_at"] == "9:00 AM"

    def test_mid_priority_also_preempted(self):
        """A mid-priority schedule preempted by a high-priority also appears."""
        low = _make_schedule(
            time(8, 0), time(10, 0), priority=1, name="Low",
            device_id="d1",
        )
        mid = _make_schedule(
            time(8, 0), time(9, 30), priority=5, name="Mid",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(9, 0), priority=10, name="High",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 8, 30, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, mid, high], now, UTC, now_playing=np)
        preempted = [r for r in result if r.get("preempted")]
        names = {r["schedule_name"] for r in preempted}
        assert "Low" in names
        assert "Mid" in names

    def test_mid_preempted_resumes_at_high_end(self):
        """Mid-priority preempted by high-priority resumes when high ends."""
        mid = _make_schedule(
            time(8, 0), time(9, 30), priority=5, name="Mid",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(9, 0), priority=10, name="High",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 8, 30, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([mid, high], now, UTC, now_playing=np)
        assert len(result) == 1
        assert result[0]["schedule_name"] == "Mid"
        assert result[0]["resumes_at"] == "9:00 AM"


# ── Group-targeted schedules ──


class TestGroupPreemption:
    """Preemption detection for group-targeted schedules."""

    def test_group_preempted_by_same_group(self):
        """A group schedule preempted by a higher-priority group schedule is shown."""
        gid = uuid.uuid4()
        low = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Low Group",
            group_id=gid,
        )
        high = _make_schedule(
            time(8, 0), time(8, 30), priority=10, name="High Group",
            group_id=gid,
        )
        now = datetime(2026, 3, 28, 8, 10, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1"), _now_playing_entry(high, "d2")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        preempted = [r for r in result if r.get("preempted")]
        assert len(preempted) == 1
        assert preempted[0]["schedule_name"] == "Low Group"
        assert preempted[0]["resumes_at"] == "8:30 AM"

    def test_partially_preempted_group_not_in_upcoming(self):
        """Group schedule still winning on some devices stays in Now Playing."""
        gid = uuid.uuid4()
        group_sched = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Group Sched",
            group_id=gid,
        )
        # Device-targeted high-priority only on d1
        device_sched = _make_schedule(
            time(8, 0), time(8, 30), priority=10, name="Device High",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 8, 10, tzinfo=timezone.utc)
        # Group schedule wins on d2, device_sched wins on d1
        np = [
            _now_playing_entry(device_sched, "d1"),
            _now_playing_entry(group_sched, "d2"),
        ]

        result = get_upcoming_schedules([group_sched, device_sched], now, UTC, now_playing=np)
        # group_sched is in winning_sids (wins on d2), so not preempted
        preempted = [r for r in result if r.get("preempted")]
        assert len(preempted) == 0


# ── Overnight schedules ──


class TestOvernightPreemption:
    """Preemption with overnight time spans."""

    def test_overnight_low_preempted(self):
        """Overnight low-priority schedule preempted by high-priority mid-night."""
        low = _make_schedule(
            time(22, 0), time(6, 0), priority=1, name="Overnight Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(23, 0), time(1, 0), priority=10, name="Late Night High",
            device_id="d1",
        )
        # It's midnight, both active
        now = datetime(2026, 3, 29, 0, 0, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        preempted = [r for r in result if r.get("preempted")]
        assert len(preempted) == 1
        assert preempted[0]["schedule_name"] == "Overnight Low"
        assert preempted[0]["resumes_at"] == "1:00 AM"


# ── Different devices (no cross-device preemption) ──


class TestDifferentDevices:
    """Schedules on different devices do not preempt each other."""

    def test_different_device_not_preempted(self):
        """A schedule on device A is not preempted by one on device B."""
        a = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Device A",
            device_id="d1",
        )
        b = _make_schedule(
            time(8, 0), time(9, 0), priority=10, name="Device B",
            device_id="d2",
        )
        now = datetime(2026, 3, 28, 8, 30, tzinfo=timezone.utc)
        # b wins on d2, a's device d1 has no winner → a not in now_playing
        np = [_now_playing_entry(b, "d2")]

        result = get_upcoming_schedules([a, b], now, UTC, now_playing=np)
        # a is _matches_now but NOT in winning_sids and no higher-priority same-target preemptor
        # → _find_resume_time returns None → excluded (not preempted, device probably offline)
        preempted = [r for r in result if r.get("preempted")]
        assert len(preempted) == 0


# ── Sorting ──


class TestPreemptedSorting:
    """Preempted entries sort correctly among future entries."""

    def test_preempted_sorted_by_resume_time(self):
        """Preempted entries sort by resume time, interleaved with future entries."""
        low = _make_schedule(
            time(8, 0), time(10, 0), priority=1, name="Preempted",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(8, 45), priority=10, name="High",
            device_id="d1",
        )
        future = _make_schedule(
            time(9, 0), time(10, 0), priority=1, name="Future",
            device_id="d2",
        )
        # At 8:30, high is playing, low is preempted (resumes at 8:45)
        # Future starts at 9:00
        now = datetime(2026, 3, 28, 8, 30, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high, future], now, UTC, now_playing=np)
        names = [r["schedule_name"] for r in result]
        # Preempted resumes at 8:45 (15 min), Future starts at 9:00 (30 min)
        assert names == ["Preempted", "Future"]


# ── _find_resume_time unit tests ──


class TestFindResumeTime:
    """Direct tests for the _find_resume_time helper."""

    def test_single_preemptor(self):
        low = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 15), time(8, 45), priority=10, name="High",
            device_id="d1",
        )
        local_now = datetime(2026, 3, 28, 8, 20)
        result = _find_resume_time(low, [low, high], local_now)
        assert result == time(8, 45)

    def test_no_preemptor_returns_none(self):
        """No higher-priority schedule → None."""
        s = _make_schedule(
            time(8, 0), time(9, 0), priority=10, name="Highest",
            device_id="d1",
        )
        local_now = datetime(2026, 3, 28, 8, 30)
        result = _find_resume_time(s, [s], local_now)
        assert result is None

    def test_same_priority_not_preemptor(self):
        """Same-priority schedules don't preempt."""
        a = _make_schedule(
            time(8, 0), time(9, 0), priority=5, name="A",
            device_id="d1",
        )
        b = _make_schedule(
            time(8, 0), time(9, 0), priority=5, name="B",
            device_id="d1",
        )
        local_now = datetime(2026, 3, 28, 8, 30)
        result = _find_resume_time(a, [a, b], local_now)
        assert result is None

    def test_different_device_not_preemptor(self):
        """Higher priority on a different device doesn't preempt."""
        low = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(9, 0), priority=10, name="High",
            device_id="d2",
        )
        local_now = datetime(2026, 3, 28, 8, 30)
        result = _find_resume_time(low, [low, high], local_now)
        assert result is None

    def test_multiple_preemptors_uses_latest_end(self):
        low = _make_schedule(
            time(8, 0), time(10, 0), priority=1, name="Low",
            device_id="d1",
        )
        mid = _make_schedule(
            time(8, 0), time(8, 30), priority=5, name="Mid",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(9, 15), priority=10, name="High",
            device_id="d1",
        )
        local_now = datetime(2026, 3, 28, 8, 20)
        result = _find_resume_time(low, [low, mid, high], local_now)
        assert result == time(9, 15)

    def test_disabled_schedule_not_preemptor(self):
        """A disabled higher-priority schedule doesn't preempt."""
        low = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(9, 0), priority=10, name="Disabled High",
            device_id="d1", enabled=False,
        )
        local_now = datetime(2026, 3, 28, 8, 30)
        result = _find_resume_time(low, [low, high], local_now)
        assert result is None

    def test_preemptor_not_active_now(self):
        """A higher-priority schedule not currently active doesn't preempt."""
        low = _make_schedule(
            time(8, 0), time(17, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(15, 0), time(16, 0), priority=10, name="Future High",
            device_id="d1",
        )
        local_now = datetime(2026, 3, 28, 10, 0)
        result = _find_resume_time(low, [low, high], local_now)
        assert result is None

    def test_overnight_preemptor(self):
        """Overnight preempting schedule correctly computes end time."""
        low = _make_schedule(
            time(22, 0), time(6, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(23, 0), time(2, 0), priority=10, name="High",
            device_id="d1",
        )
        local_now = datetime(2026, 3, 29, 0, 30)  # 12:30 AM
        result = _find_resume_time(low, [low, high], local_now)
        assert result == time(2, 0)


# ── Timezone-aware preemption ──


class TestTimezonePreemption:
    """Preemption works correctly in non-UTC timezones."""

    def test_pacific_timezone_preemption(self):
        """Preemption detected correctly in Pacific timezone."""
        low = _make_schedule(
            time(10, 0), time(12, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(10, 0), time(11, 0), priority=10, name="High",
            device_id="d1",
        )
        # 5:30 PM UTC = 10:30 AM Pacific
        now = datetime(2026, 3, 28, 17, 30, tzinfo=timezone.utc)
        tz = ZoneInfo("America/Los_Angeles")
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, tz, now_playing=np)
        assert len(result) == 1
        assert result[0]["schedule_name"] == "Low"
        assert result[0]["preempted"] is True
        assert result[0]["resumes_at"] == "11:00 AM"

    def test_timezone_matches_now_uses_local_time(self):
        """_matches_now in get_upcoming_schedules uses local time, not UTC."""
        # Schedule 9-17 local (Pacific). At 5 PM UTC = 10 AM Pacific, it IS active.
        s = _make_schedule(
            time(9, 0), time(17, 0), priority=1, name="Pacific Active",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 17, 0, tzinfo=timezone.utc)
        tz = ZoneInfo("America/Los_Angeles")
        np = [_now_playing_entry(s, "d1")]

        # Schedule is winning on d1, so it should NOT be in upcoming
        result = get_upcoming_schedules([s], now, tz, now_playing=np)
        assert len(result) == 0


# ── Preempted entry fields ──


class TestPreemptedEntryFields:
    """Verify all fields in a preempted entry are correct."""

    def test_full_entry_fields(self):
        """Preempted entry has all expected fields."""
        low = _make_schedule(
            time(8, 0), time(10, 0), priority=1, name="Low Priority",
            device_id="d1", asset_filename="background.mp4",
        )
        high = _make_schedule(
            time(8, 30), time(9, 0), priority=10, name="High Priority",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 8, 35, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        assert len(result) == 1
        entry = result[0]

        assert entry["schedule_name"] == "Low Priority"
        assert entry["asset_filename"] == "background.mp4"
        assert entry["start_time"] == "8:00 AM"
        assert entry["end_time"] == "10:00 AM"
        assert entry["duration_mins"] == 120
        assert entry["preempted"] is True
        assert entry["resumes_at"] == "9:00 AM"
        assert entry["day_label"] == "today"
        assert entry["target_type"] == "device"
        assert "resumes in" in entry["countdown"]

    def test_resume_countdown_less_than_minute(self):
        """When resume is <1 minute away, countdown says so."""
        low = _make_schedule(
            time(8, 0), time(9, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(8, 30), priority=10, name="High",
            device_id="d1",
        )
        # 29 seconds before 8:30
        now = datetime(2026, 3, 28, 8, 29, 31, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        assert result[0]["countdown"] == "resumes in less than a minute"

    def test_resume_countdown_hours(self):
        """When resume is hours away, countdown shows hours."""
        low = _make_schedule(
            time(8, 0), time(17, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(12, 0), priority=10, name="High",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 9, 30, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        assert result[0]["countdown"] == "resumes in 2 hours, 30 minutes"

    def test_resume_countdown_singular(self):
        """Singular forms for 1 hour/minute."""
        low = _make_schedule(
            time(8, 0), time(17, 0), priority=1, name="Low",
            device_id="d1",
        )
        high = _make_schedule(
            time(8, 0), time(9, 1), priority=10, name="High",
            device_id="d1",
        )
        now = datetime(2026, 3, 28, 9, 0, tzinfo=timezone.utc)
        np = [_now_playing_entry(high, "d1")]

        result = get_upcoming_schedules([low, high], now, UTC, now_playing=np)
        assert result[0]["countdown"] == "resumes in 1 minute"
