"""Tests for the occurrence-based schedule window engine (Stage 0).

These cover the corner cases the pre-Stage-0 decomposed predicate got wrong:
windows crossing midnight across weekday/date boundaries, and sub-minute precision.
"""

import uuid
from datetime import datetime, time, timezone

from cms.models.schedule import Schedule
from cms.services.occurrence import (
    matches_at,
    occurrence_bounds,
    schedules_overlap,
)
from cms.services.scheduler import schedules_conflict


def _sched(
    start_time,
    end_time,
    *,
    group_id=None,
    priority=0,
    days_of_week=None,
    start_date=None,
    end_date=None,
    enabled=True,
) -> Schedule:
    return Schedule(
        name="test",
        asset_id=uuid.uuid4(),
        group_id=group_id or uuid.uuid4(),
        enabled=enabled,
        start_time=start_time,
        end_time=end_time,
        days_of_week=days_of_week,
        start_date=start_date,
        end_date=end_date,
        priority=priority,
    )


class TestOccurrenceBounds:
    def test_normal_window(self):
        assert occurrence_bounds(time(9, 0), time(17, 0)) == (
            9 * 3600 * 1_000_000,
            17 * 3600 * 1_000_000,
        )

    def test_overnight_window_shifts_end_by_a_day(self):
        s, e = occurrence_bounds(time(22, 0), time(2, 0))
        assert s == 22 * 3600 * 1_000_000
        assert e == (2 + 24) * 3600 * 1_000_000

    def test_zero_length_window_is_none(self):
        assert occurrence_bounds(time(12, 0), time(12, 0)) is None

    def test_sub_minute_precision_preserved(self):
        s, e = occurrence_bounds(time(10, 0, 30), time(10, 0, 45))
        assert e - s == 15 * 1_000_000


class TestSchedulesOverlapOvernight:
    def test_overnight_conflict_across_adjacent_weekdays(self):
        """The canonical bug: Mon 22:00-02:00 overlaps Tue 01:00-03:00 after midnight."""
        gid = uuid.uuid4()
        a = _sched(time(22, 0), time(2, 0), group_id=gid, days_of_week=[1])  # Mon
        b = _sched(time(1, 0), time(3, 0), group_id=gid, days_of_week=[2])   # Tue
        assert schedules_overlap(a, b) is True
        assert schedules_conflict(a, b) is True

    def test_overnight_no_conflict_when_pre_midnight_only(self):
        """Mon 22:00-23:00 does NOT reach Tuesday, so no overlap with Tue 01:00-03:00."""
        gid = uuid.uuid4()
        a = _sched(time(22, 0), time(23, 0), group_id=gid, days_of_week=[1])
        b = _sched(time(1, 0), time(3, 0), group_id=gid, days_of_week=[2])
        assert schedules_overlap(a, b) is False
        assert schedules_conflict(a, b) is False

    def test_overnight_conflict_across_date_boundary(self):
        """Aug 31 (Mon) 22:00-02:00 overlaps Sep 1 (Tue) 01:00-03:00 despite disjoint date ranges."""
        gid = uuid.uuid4()
        a = _sched(
            time(22, 0), time(2, 0), group_id=gid,
            start_date=datetime(2026, 8, 31, tzinfo=timezone.utc),
            end_date=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
        b = _sched(
            time(1, 0), time(3, 0), group_id=gid,
            start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        assert schedules_conflict(a, b) is True

    def test_two_overnight_windows_same_group_conflict(self):
        gid = uuid.uuid4()
        a = _sched(time(22, 0), time(4, 0), group_id=gid)
        b = _sched(time(23, 0), time(6, 0), group_id=gid)
        assert schedules_conflict(a, b) is True

    def test_disjoint_date_ranges_no_conflict_when_not_crossing_midnight(self):
        gid = uuid.uuid4()
        a = _sched(
            time(9, 0), time(12, 0), group_id=gid,
            start_date=datetime(2026, 8, 31, tzinfo=timezone.utc),
            end_date=datetime(2026, 8, 31, tzinfo=timezone.utc),
        )
        b = _sched(
            time(9, 0), time(12, 0), group_id=gid,
            start_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 9, 1, tzinfo=timezone.utc),
        )
        assert schedules_conflict(a, b) is False


class TestSchedulesOverlapSubMinute:
    def test_sub_minute_no_overlap(self):
        gid = uuid.uuid4()
        a = _sched(time(10, 0, 30), time(10, 1, 0), group_id=gid)
        b = _sched(time(10, 0, 0), time(10, 0, 20), group_id=gid)
        assert schedules_conflict(a, b) is False

    def test_sub_minute_overlap(self):
        gid = uuid.uuid4()
        a = _sched(time(10, 0, 30), time(10, 1, 0), group_id=gid)
        b = _sched(time(10, 0, 10), time(10, 0, 40), group_id=gid)
        assert schedules_conflict(a, b) is True


class TestMatchesAtPostMidnight:
    def test_weekday_scoped_overnight_matches_after_midnight(self):
        """Mon-only 22:00-02:00 is still active Tuesday 01:00 (occurrence anchored Mon)."""
        # 2026-08-31 is a Monday; 2026-09-01 Tuesday.
        s = _sched(time(22, 0), time(2, 0), days_of_week=[1])
        assert matches_at(s, datetime(2026, 9, 1, 1, 0)) is True

    def test_weekday_scoped_overnight_stops_at_end(self):
        s = _sched(time(22, 0), time(2, 0), days_of_week=[1])
        assert matches_at(s, datetime(2026, 9, 1, 2, 0)) is False
        assert matches_at(s, datetime(2026, 9, 1, 3, 0)) is False

    def test_weekday_scoped_no_match_on_wrong_evening(self):
        """Mon-only window must not fire Tuesday evening."""
        s = _sched(time(22, 0), time(2, 0), days_of_week=[1])
        assert matches_at(s, datetime(2026, 9, 1, 22, 30)) is False

    def test_one_shot_overnight_continues_past_midnight(self):
        s = _sched(
            time(22, 0), time(2, 0),
            start_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
            end_date=datetime(2026, 4, 1, tzinfo=timezone.utc),
        )
        assert matches_at(s, datetime(2026, 4, 2, 1, 0)) is True
        assert matches_at(s, datetime(2026, 4, 2, 2, 30)) is False
