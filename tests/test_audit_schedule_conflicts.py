"""Unit tests for the read-only schedule-conflict audit helpers.

The audit script talks to a live CMS, but its overlap arithmetic is pure and
worth pinning down -- particularly the overnight-window case, which is the
easiest thing to get wrong and the most expensive to get wrong silently.
"""

import importlib.util
from pathlib import Path

import pytest

_SPEC = importlib.util.spec_from_file_location(
    "audit_schedule_conflicts",
    Path(__file__).resolve().parents[1] / "scripts" / "audit_schedule_conflicts.py",
)
audit = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(audit)


class TestWindowsOverlap:
    @pytest.mark.parametrize(
        "a_start,a_end,b_start,b_end,expected",
        [
            ("08:00", "12:00", "11:00", "13:00", True),   # partial overlap
            ("08:00", "12:00", "12:00", "14:00", False),  # touching, not overlapping
            ("08:00", "12:00", "13:00", "14:00", False),  # disjoint
            ("08:00", "18:00", "09:00", "10:00", True),   # containment
            ("22:00", "02:00", "01:00", "03:00", True),   # overnight vs morning
            ("22:00", "02:00", "03:00", "21:00", False),  # overnight, still disjoint
            ("22:00", "02:00", "23:00", "23:30", True),   # overnight, late evening
            ("00:00", "24:00", "12:00", "13:00", True),   # full-day window
        ],
    )
    def test_overlap(self, a_start, a_end, b_start, b_end, expected):
        assert audit._windows_overlap(a_start, a_end, b_start, b_end) is expected

    def test_overlap_is_symmetric(self):
        assert audit._windows_overlap("22:00", "02:00", "01:00", "03:00") is True
        assert audit._windows_overlap("01:00", "03:00", "22:00", "02:00") is True


class TestDaysOverlap:
    def test_empty_means_every_day(self):
        assert audit._days_overlap(None, [3]) is True
        assert audit._days_overlap([], [3]) is True

    def test_disjoint_days_do_not_overlap(self):
        assert audit._days_overlap([1, 2], [3, 4]) is False

    def test_shared_day_overlaps(self):
        assert audit._days_overlap([1, 2], [2, 3]) is True


class TestDatesOverlap:
    def test_open_ended_ranges_always_overlap(self):
        assert audit._dates_overlap({}, {}) is True

    def test_separated_ranges_do_not_overlap(self):
        a = {"start_date": "2026-01-01", "end_date": "2026-01-31"}
        b = {"start_date": "2026-02-01", "end_date": "2026-02-28"}
        assert audit._dates_overlap(a, b) is False
        assert audit._dates_overlap(b, a) is False

    def test_touching_ranges_overlap(self):
        a = {"start_date": "2026-01-01", "end_date": "2026-02-01"}
        b = {"start_date": "2026-02-01", "end_date": "2026-02-28"}
        assert audit._dates_overlap(a, b) is True
