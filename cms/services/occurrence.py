"""Occurrence-based schedule window model.

A schedule defines a *recurring set of half-open datetime intervals* ("occurrences").
Each occurrence is anchored on a calendar day whose ISO weekday is allowed by
``days_of_week`` (empty/None = every day) and which falls within the inclusive
``[start_date, end_date]`` day range (None = unbounded). The occurrence interval is::

    [ anchor 00:00 + start_time ,  anchor 00:00 + end_time (+1 day if it wraps past midnight) )

This is the single source of truth for:
  * whether an instant falls inside a schedule (``matches_at``), and
  * whether two schedules' occurrences ever intersect (``schedules_overlap``).

Decomposing overlap into independent time/day/date predicates (the pre-Stage-0
approach) is INCORRECT for windows that cross midnight: the calendar day an
occurrence "belongs to" shifts, so e.g. ``Mon 22:00-02:00`` and ``Tue 01:00-03:00``
actually overlap on Tuesday morning even though their weekday sets are disjoint.

All arithmetic uses microsecond resolution (times may carry seconds), so sub-minute
windows are compared exactly rather than being truncated to whole minutes.

Semantics notes:
  * ``end_time == start_time`` is a zero-length window → NO occurrences (never plays).
    (The API forbids creating one; this keeps parity for directly-constructed rows.)
  * A wrapping window (``end_time < start_time``) has duration ``< 24h`` by construction,
    so every occurrence spans at most two adjacent calendar days. Hence two overlapping
    occurrences have anchor days at most one day apart (``delta in {-1, 0, +1}``).
  * Conflict detection operates in the schedules' shared nominal wall-clock calendar
    (the frame in which ``start_time``/``days_of_week``/``start_date`` are authored).
    Per-device timezone execution is handled at sync-time, not here.
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

DAY_US = 86_400_000_000
_ALL_WEEKDAYS = frozenset(range(1, 8))


def _time_to_us(t: time) -> int:
    """Microseconds since midnight for a ``time``."""
    return ((t.hour * 3600 + t.minute * 60 + t.second) * 1_000_000) + t.microsecond


def occurrence_bounds(start_time: time, end_time: time) -> tuple[int, int] | None:
    """Return ``(start_us, end_us)`` for one occurrence relative to its anchor midnight.

    ``end_us`` is shifted by one day when the window wraps past midnight, so the
    interval is always ``start_us < end_us``. Returns ``None`` for a zero-length
    (``start == end``) window.
    """
    start_us = _time_to_us(start_time)
    end_us = _time_to_us(end_time)
    if end_us == start_us:
        return None
    if end_us < start_us:
        end_us += DAY_US
    return start_us, end_us


def allowed_weekdays(days_of_week: list[int] | None) -> frozenset[int]:
    """ISO weekday set the schedule may anchor on. Empty/None means every day."""
    if not days_of_week:
        return _ALL_WEEKDAYS
    return frozenset(days_of_week) & _ALL_WEEKDAYS


def _date_of(value: datetime | date | None) -> date | None:
    if value is None:
        return None
    return value.date() if isinstance(value, datetime) else value


def _shift_weekday(weekday: int, delta: int) -> int:
    """ISO weekday ``delta`` days after ``weekday`` (1=Mon..7=Sun)."""
    return ((weekday - 1 + delta) % 7) + 1


def _range_contains_weekday(lo: date | None, hi: date | None, weekdays: frozenset[int]) -> bool:
    """Does some date in the inclusive ``[lo, hi]`` range fall on an allowed weekday?

    ``None`` bounds are unbounded (±infinity). An unbounded-or-week-long span always
    contains every weekday, so any non-empty ``weekdays`` is satisfied.
    """
    if not weekdays:
        return False
    if lo is None or hi is None:
        return True  # at least one side unbounded → infinitely many dates
    if lo > hi:
        return False
    if (hi - lo).days >= 6:
        return True  # a full week is covered → every weekday appears
    d = lo
    while d <= hi:
        if d.isoweekday() in weekdays:
            return True
        d += timedelta(days=1)
    return False


def matches_at(schedule, now: datetime) -> bool:
    """True if ``schedule`` has an occurrence covering the instant ``now`` (half-open).

    Considers both today's anchor and yesterday's anchor (a window that wrapped past
    midnight is still the *previous* day's occurrence in the small hours).
    """
    if not getattr(schedule, "enabled", True):
        return False
    bounds = occurrence_bounds(schedule.start_time, schedule.end_time)
    if bounds is None:
        return False
    start_us, end_us = bounds
    days = allowed_weekdays(schedule.days_of_week)
    lo = _date_of(schedule.start_date)
    hi = _date_of(schedule.end_date)

    now_date = now.date()
    now_us = (
        (now.hour * 3600 + now.minute * 60 + now.second) * 1_000_000 + now.microsecond
    )
    for anchor in (now_date, now_date - timedelta(days=1)):
        if anchor.isoweekday() not in days:
            continue
        if lo is not None and anchor < lo:
            continue
        if hi is not None and anchor > hi:
            continue
        offset_us = now_us + (0 if anchor == now_date else DAY_US)
        if start_us <= offset_us < end_us:
            return True
    return False


def schedules_overlap(a, b) -> bool:
    """True if any occurrence of schedule ``a`` intersects any occurrence of ``b``.

    Pure temporal test — ignores group and priority. Correct across midnight, date
    boundaries, and sub-minute windows.
    """
    a_bounds = occurrence_bounds(a.start_time, a.end_time)
    b_bounds = occurrence_bounds(b.start_time, b.end_time)
    if a_bounds is None or b_bounds is None:
        return False

    a_start, a_end = a_bounds
    b_start0, b_end0 = b_bounds
    a_days = allowed_weekdays(a.days_of_week)
    b_days = allowed_weekdays(b.days_of_week)
    a_lo, a_hi = _date_of(a.start_date), _date_of(a.end_date)
    b_lo, b_hi = _date_of(b.start_date), _date_of(b.end_date)

    # Each occurrence spans < 48h, so only anchor-day offsets of -1/0/+1 can intersect.
    for delta in (-1, 0, 1):
        b_start = b_start0 + delta * DAY_US
        b_end = b_end0 + delta * DAY_US
        if not (a_start < b_end and b_start < a_end):
            continue  # time windows don't intersect under this day offset

        # Weekdays w on which A may anchor such that B (anchored delta days later) is allowed.
        common = frozenset(
            w for w in a_days if _shift_weekday(w, delta) in b_days
        )
        if not common:
            continue

        # A anchors dA in A's range; B anchors dA+delta in B's range
        # → dA in [b_lo - delta, b_hi - delta]. Intersect with A's range.
        lo = _max_date(a_lo, _shift_date(b_lo, -delta))
        hi = _min_date(a_hi, _shift_date(b_hi, -delta))
        if _range_contains_weekday(lo, hi, common):
            return True
    return False


def _shift_date(value: date | None, days: int) -> date | None:
    return None if value is None else value + timedelta(days=days)


def _max_date(x: date | None, y: date | None) -> date | None:
    """Lower-bound max where ``None`` means -infinity."""
    if x is None:
        return y
    if y is None:
        return x
    return max(x, y)


def _min_date(x: date | None, y: date | None) -> date | None:
    """Upper-bound min where ``None`` means +infinity."""
    if x is None:
        return y
    if y is None:
        return x
    return min(x, y)
