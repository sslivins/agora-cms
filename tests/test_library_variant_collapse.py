"""Tests for the Library variant-collapse helper."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

import pytest

from cms.services.variant_view import collapse_to_latest


@dataclass
class _V:
    """Lightweight stand-in for AssetVariant.

    Only the attributes collapse_to_latest reads are populated.
    """
    profile_id: UUID
    created_at: datetime
    deleted_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)
    label: str = ""


NOW = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)


def _mk(profile: UUID, offset_minutes: int, *, deleted: bool = False, label: str = "") -> _V:
    return _V(
        profile_id=profile,
        created_at=NOW + timedelta(minutes=offset_minutes),
        deleted_at=(NOW + timedelta(minutes=offset_minutes + 1)) if deleted else None,
        label=label,
    )


class TestCollapseToLatest:
    def test_empty_input(self):
        assert collapse_to_latest([]) == []

    def test_single_variant_passes_through(self):
        p = uuid4()
        v = _mk(p, 0, label="only")
        assert collapse_to_latest([v]) == [v]

    def test_multiple_profiles_each_kept(self):
        p1, p2 = uuid4(), uuid4()
        a = _mk(p1, 0, label="p1")
        b = _mk(p2, 0, label="p2")
        out = collapse_to_latest([a, b])
        assert {v.label for v in out} == {"p1", "p2"}

    def test_collapses_to_newest_per_profile(self):
        p = uuid4()
        old = _mk(p, 0, label="old")
        new = _mk(p, 10, label="new")
        out = collapse_to_latest([old, new])
        assert [v.label for v in out] == ["new"]

    def test_newest_wins_regardless_of_input_order(self):
        p = uuid4()
        old = _mk(p, 0, label="old")
        new = _mk(p, 10, label="new")
        assert [v.label for v in collapse_to_latest([new, old])] == ["new"]

    def test_soft_deleted_rows_are_dropped(self):
        p = uuid4()
        dead_newer = _mk(p, 20, deleted=True, label="dead_newer")
        alive_older = _mk(p, 0, label="alive_older")
        out = collapse_to_latest([dead_newer, alive_older])
        assert [v.label for v in out] == ["alive_older"]

    def test_all_soft_deleted_for_profile_drops_slot(self):
        p = uuid4()
        out = collapse_to_latest([_mk(p, 0, deleted=True), _mk(p, 10, deleted=True)])
        assert out == []

    def test_cancelled_newest_is_shown(self):
        """Newest row is kept even if it's CANCELLED.

        Soft-deleted-ness (not cancelled-ness) is the gate for hiding
        variants.  A CANCELLED row that hasn't been reaped yet is still
        the honest "current state" for that profile slot.
        """
        p = uuid4()
        ready_old = _mk(p, 0, label="ready_old")
        cancelled_new = _mk(p, 10, label="cancelled_new")
        out = collapse_to_latest([ready_old, cancelled_new])
        assert [v.label for v in out] == ["cancelled_new"]

    def test_in_flight_replaces_ready_sibling(self):
        """Matches the prod scenario: edit profile → new PENDING created,
        old READY still in DB until supersede.  UI should show PENDING."""
        p = uuid4()
        ready_old = _mk(p, 0, label="ready_old")
        pending_new = _mk(p, 5, label="pending_new")
        out = collapse_to_latest([ready_old, pending_new])
        assert [v.label for v in out] == ["pending_new"]

    def test_deterministic_tie_break_by_id(self):
        p = uuid4()
        t = NOW
        id_low = UUID("00000000-0000-0000-0000-000000000001")
        id_high = UUID("ffffffff-ffff-ffff-ffff-ffffffffffff")
        low = _V(profile_id=p, created_at=t, id=id_low, label="low")
        high = _V(profile_id=p, created_at=t, id=id_high, label="high")
        assert [v.label for v in collapse_to_latest([low, high])] == ["high"]
        assert [v.label for v in collapse_to_latest([high, low])] == ["high"]

    def test_none_profile_id_dropped(self):
        p = uuid4()
        orphan = _V(profile_id=None, created_at=NOW, label="orphan")  # type: ignore[arg-type]
        keep = _mk(p, 0, label="keep")
        out = collapse_to_latest([orphan, keep])
        assert [v.label for v in out] == ["keep"]
