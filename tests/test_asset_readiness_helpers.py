"""Tests for the asset-readiness helpers (issue #201).

Covers ``collapse_to_ready`` and ``is_asset_ready`` in
``cms.services.variant_view`` — the rule that gates splash-screen / schedule
asset selection on "every profile has a live READY variant".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from uuid import UUID, uuid4

from cms.services.variant_view import collapse_to_ready, is_asset_ready
from shared.models.asset import VariantStatus


@dataclass
class _V:
    """Lightweight stand-in for AssetVariant.

    Only the attributes the helpers actually read are populated.
    """

    profile_id: UUID
    created_at: datetime
    status: VariantStatus
    deleted_at: datetime | None = None
    id: UUID = field(default_factory=uuid4)

    def __hash__(self) -> int:
        return hash(self.id)


NOW = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)


def _mk(
    profile: UUID,
    status: VariantStatus,
    *,
    offset_minutes: int = 0,
    deleted: bool = False,
) -> _V:
    return _V(
        profile_id=profile,
        created_at=NOW + timedelta(minutes=offset_minutes),
        status=status,
        deleted_at=(NOW + timedelta(minutes=offset_minutes + 1)) if deleted else None,
    )


class TestCollapseToReady:
    def test_empty(self):
        assert collapse_to_ready([]) == []

    def test_filters_non_ready(self):
        p = uuid4()
        vs = [_mk(p, VariantStatus.PROCESSING), _mk(p, VariantStatus.PENDING)]
        assert collapse_to_ready(vs) == []

    def test_returns_ready_variant(self):
        p = uuid4()
        v = _mk(p, VariantStatus.READY)
        assert collapse_to_ready([v]) == [v]

    def test_one_per_profile(self):
        p1, p2 = uuid4(), uuid4()
        v1 = _mk(p1, VariantStatus.READY)
        v2 = _mk(p2, VariantStatus.READY)
        out = collapse_to_ready([v1, v2])
        assert set(out) == {v1, v2}

    def test_newest_wins_per_profile(self):
        p = uuid4()
        older = _mk(p, VariantStatus.READY, offset_minutes=0)
        newer = _mk(p, VariantStatus.READY, offset_minutes=5)
        out = collapse_to_ready([older, newer])
        assert out == [newer]

    def test_ignores_soft_deleted(self):
        p = uuid4()
        live = _mk(p, VariantStatus.READY)
        dead = _mk(p, VariantStatus.READY, offset_minutes=5, deleted=True)
        out = collapse_to_ready([live, dead])
        assert out == [live]

    def test_ignores_rows_with_no_profile(self):
        v = _V(
            profile_id=None,  # type: ignore[arg-type]
            created_at=NOW,
            status=VariantStatus.READY,
        )
        assert collapse_to_ready([v]) == []


class TestIsAssetReady:
    def test_no_variants_is_ready(self):
        """Webpage/stream/etc. assets have zero variants → always ready."""
        assert is_asset_ready([]) == (True, None)

    def test_all_ready_is_ready(self):
        p1, p2 = uuid4(), uuid4()
        vs = [_mk(p1, VariantStatus.READY), _mk(p2, VariantStatus.READY)]
        assert is_asset_ready(vs) == (True, None)

    def test_one_profile_still_processing_blocks(self):
        p1, p2 = uuid4(), uuid4()
        vs = [_mk(p1, VariantStatus.READY), _mk(p2, VariantStatus.PROCESSING)]
        ready, reason = is_asset_ready(vs)
        assert ready is False
        assert reason == "transcoding…"

    def test_one_profile_pending_blocks(self):
        p1, p2 = uuid4(), uuid4()
        vs = [_mk(p1, VariantStatus.READY), _mk(p2, VariantStatus.PENDING)]
        ready, reason = is_asset_ready(vs)
        assert ready is False
        assert reason == "transcoding…"

    def test_all_profiles_failed_reports_failed(self):
        p = uuid4()
        vs = [_mk(p, VariantStatus.FAILED)]
        ready, reason = is_asset_ready(vs)
        assert ready is False
        assert reason == "transcode failed"

    def test_in_flight_wins_over_failed_mixed(self):
        """If at least one unready profile is still in flight, that's the
        user-facing reason (hope lives)."""
        p1, p2 = uuid4(), uuid4()
        vs = [_mk(p1, VariantStatus.FAILED), _mk(p2, VariantStatus.PROCESSING)]
        ready, reason = is_asset_ready(vs)
        assert ready is False
        assert reason == "transcoding…"

    def test_retranscode_case_stays_ready(self):
        """Old READY + new PROCESSING for the same profile → still ready.

        The old blob keeps serving traffic until the new one lands.
        """
        p1, p2 = uuid4(), uuid4()
        vs = [
            _mk(p1, VariantStatus.READY, offset_minutes=0),
            _mk(p1, VariantStatus.PROCESSING, offset_minutes=10),
            _mk(p2, VariantStatus.READY),
        ]
        assert is_asset_ready(vs) == (True, None)

    def test_retranscode_superseded_old_still_ready(self):
        """Same as above, but the old variant has been soft-deleted —
        that removes the playable fallback, so the asset is NOT ready."""
        p = uuid4()
        vs = [
            _mk(p, VariantStatus.READY, offset_minutes=0, deleted=True),
            _mk(p, VariantStatus.PROCESSING, offset_minutes=10),
        ]
        ready, reason = is_asset_ready(vs)
        assert ready is False
        assert reason == "transcoding…"

    def test_soft_deleted_only_is_ready(self):
        """All variants soft-deleted → no live profiles to check → ready.

        (This is effectively the "no variants" case.)
        """
        p = uuid4()
        vs = [_mk(p, VariantStatus.READY, deleted=True)]
        assert is_asset_ready(vs) == (True, None)

    def test_partial_ready_only_counts_live_variants(self):
        """One profile ready, another has a soft-deleted PROCESSING row
        → that second profile has no live variants → doesn't count."""
        p1, p2 = uuid4(), uuid4()
        vs = [
            _mk(p1, VariantStatus.READY),
            _mk(p2, VariantStatus.PROCESSING, deleted=True),
        ]
        assert is_asset_ready(vs) == (True, None)
