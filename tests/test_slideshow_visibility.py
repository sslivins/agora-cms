"""Per-slide visibility-window tests (agora per-slide-visibility).

Covers the three layers of the feature:

* :func:`cms.services.slideshow_resolver._slide_window_open` — the pure
  device-local-time predicate (date range / time-of-day / weekday /
  overnight-wrap / mixtures).
* :class:`cms.schemas.asset.SlideIn` validators — ``active_days``
  normalisation and the cross-field window-coherence checks that mirror
  the DB CHECK constraints.
* :func:`cms.services.slideshow_resolver._load_slide_specs` — closed
  slides are dropped from the resolved deck when a device-local now is
  supplied, and ``local_now is None`` (the builder/readiness path) shows
  every slide unchanged.

The predicate + validator suites are pure functions (no DB); only the
``_load_slide_specs`` suite touches the session fixture.
"""

from __future__ import annotations

from datetime import date, datetime, time, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cms.models.asset import Asset, AssetType
from cms.models.slideshow_slide import SlideshowSlide
from cms.schemas.asset import SlideIn
from cms.services.slideshow_resolver import _load_slide_specs, _slide_window_open

# An ``asset`` kind slide requires a ``source_asset_id`` (model validator),
# so every ``SlideIn(kind="asset", ...)`` below pins this dummy id.  The
# window validators run BEFORE the kind/columns validator, so the
# rejection tests still fail on the window check (the right reason) — this
# only keeps the *success* cases from tripping the source-required rule.
SID = uuid4()


# ---------------------------------------------------------------------------
# _slide_window_open — pure predicate
# ---------------------------------------------------------------------------

def _row(**kw):
    """A minimal slide-row stub exposing only the five window columns.

    ``_slide_window_open`` reads nothing else, so a ``SimpleNamespace`` is
    faithful and keeps these tests DB-free.
    """
    defaults = dict(
        valid_from=None,
        valid_to=None,
        active_days=None,
        active_start=None,
        active_end=None,
    )
    defaults.update(kw)
    return SimpleNamespace(**defaults)


def _dt(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


class TestSlideWindowOpen:
    def test_no_window_is_always_open(self):
        assert _slide_window_open(_row(), _dt(2026, 12, 25, 3, 0)) is True

    def test_date_range_inclusive_both_ends(self):
        row = _row(valid_from=date(2026, 12, 1), valid_to=date(2026, 12, 26))
        assert _slide_window_open(row, _dt(2026, 11, 30)) is False  # day before
        assert _slide_window_open(row, _dt(2026, 12, 1)) is True   # start incl.
        assert _slide_window_open(row, _dt(2026, 12, 26)) is True  # end incl.
        assert _slide_window_open(row, _dt(2026, 12, 27)) is False  # day after

    def test_only_valid_from(self):
        row = _row(valid_from=date(2026, 6, 1))
        assert _slide_window_open(row, _dt(2026, 5, 31)) is False
        assert _slide_window_open(row, _dt(2026, 6, 1)) is True
        assert _slide_window_open(row, _dt(2030, 1, 1)) is True

    def test_only_valid_to(self):
        row = _row(valid_to=date(2026, 6, 1))
        assert _slide_window_open(row, _dt(2026, 6, 1)) is True
        assert _slide_window_open(row, _dt(2026, 6, 2)) is False

    def test_normal_time_window_start_incl_end_excl(self):
        # 13:00 inclusive .. 14:00 exclusive.
        row = _row(active_start=time(13, 0), active_end=time(14, 0))
        assert _slide_window_open(row, _dt(2026, 6, 1, 12, 59)) is False
        assert _slide_window_open(row, _dt(2026, 6, 1, 13, 0)) is True
        assert _slide_window_open(row, _dt(2026, 6, 1, 13, 59)) is True
        assert _slide_window_open(row, _dt(2026, 6, 1, 14, 0)) is False  # excl.

    def test_only_active_start(self):
        row = _row(active_start=time(9, 0))
        assert _slide_window_open(row, _dt(2026, 6, 1, 8, 59)) is False
        assert _slide_window_open(row, _dt(2026, 6, 1, 9, 0)) is True

    def test_only_active_end(self):
        row = _row(active_end=time(17, 0))
        assert _slide_window_open(row, _dt(2026, 6, 1, 16, 59)) is True
        assert _slide_window_open(row, _dt(2026, 6, 1, 17, 0)) is False

    def test_overnight_wrap_window(self):
        # 22:00 .. 02:00 spans midnight.
        row = _row(active_start=time(22, 0), active_end=time(2, 0))
        assert _slide_window_open(row, _dt(2026, 6, 1, 21, 59)) is False
        assert _slide_window_open(row, _dt(2026, 6, 1, 22, 0)) is True
        assert _slide_window_open(row, _dt(2026, 6, 1, 23, 30)) is True
        assert _slide_window_open(row, _dt(2026, 6, 2, 1, 59)) is True
        assert _slide_window_open(row, _dt(2026, 6, 2, 2, 0)) is False

    def test_weekday_only(self):
        # 2026-06-01 is a Monday (weekday 0).
        row = _row(active_days=[0, 2, 4])  # Mon, Wed, Fri
        assert _slide_window_open(row, _dt(2026, 6, 1)) is True   # Mon
        assert _slide_window_open(row, _dt(2026, 6, 2)) is False  # Tue
        assert _slide_window_open(row, _dt(2026, 6, 3)) is True   # Wed

    def test_empty_weekday_list_is_every_day(self):
        # Resolver treats an empty list the same as None (no restriction).
        assert _slide_window_open(_row(active_days=[]), _dt(2026, 6, 2)) is True

    def test_wrap_tail_belongs_to_opening_days_weekday(self):
        # Fri 22:00 .. Sat 02:00, allowed only on Friday (weekday 4).
        # 2026-06-05 is a Friday; 2026-06-06 is a Saturday.
        row = _row(active_start=time(22, 0), active_end=time(2, 0), active_days=[4])
        # Friday night inside the window -> open.
        assert _slide_window_open(row, _dt(2026, 6, 5, 23, 0)) is True
        # Saturday 00:30 is the wrap tail -> effective weekday is Friday -> open.
        assert _slide_window_open(row, _dt(2026, 6, 6, 0, 30)) is True
        # Saturday 22:00 is a *fresh* opening on Saturday -> not allowed.
        assert _slide_window_open(row, _dt(2026, 6, 6, 22, 0)) is False

    def test_full_mixture_all_constraints(self):
        # Christmas-week flash sale: Dec 1-26, 1-2pm, weekdays only.
        row = _row(
            valid_from=date(2026, 12, 1),
            valid_to=date(2026, 12, 26),
            active_start=time(13, 0),
            active_end=time(14, 0),
            active_days=[0, 1, 2, 3, 4],
        )
        # 2026-12-04 is a Friday (in range, weekday ok), 13:30 in window.
        assert _slide_window_open(row, _dt(2026, 12, 4, 13, 30)) is True
        # Right day/time but out of date range.
        assert _slide_window_open(row, _dt(2026, 11, 27, 13, 30)) is False
        # In range + weekday but wrong time.
        assert _slide_window_open(row, _dt(2026, 12, 4, 9, 0)) is False
        # In range + time but a weekend (2026-12-05 is Saturday).
        assert _slide_window_open(row, _dt(2026, 12, 5, 13, 30)) is False


# ---------------------------------------------------------------------------
# SlideIn validators
# ---------------------------------------------------------------------------

class TestSlideInValidators:
    def test_active_days_deduped_and_sorted(self):
        s = SlideIn(source_asset_id=SID, kind="asset", active_days=[4, 0, 4, 2])
        assert s.active_days == [0, 2, 4]

    def test_active_days_empty_becomes_none(self):
        s = SlideIn(source_asset_id=SID, kind="asset", active_days=[])
        assert s.active_days is None

    def test_active_days_out_of_range_rejected(self):
        with pytest.raises(ValueError):
            SlideIn(source_asset_id=SID, kind="asset", active_days=[7])
        with pytest.raises(ValueError):
            SlideIn(source_asset_id=SID, kind="asset", active_days=[-1])

    def test_valid_to_before_valid_from_rejected(self):
        with pytest.raises(ValueError):
            SlideIn(
                source_asset_id=SID,
                kind="asset",
                valid_from=date(2026, 12, 26),
                valid_to=date(2026, 12, 1),
            )

    def test_single_day_window_allowed(self):
        s = SlideIn(
            source_asset_id=SID,
            kind="asset",
            valid_from=date(2026, 12, 25),
            valid_to=date(2026, 12, 25),
        )
        assert s.valid_from == s.valid_to == date(2026, 12, 25)

    def test_equal_start_end_time_rejected(self):
        with pytest.raises(ValueError):
            SlideIn(
                source_asset_id=SID,
                kind="asset",
                active_start=time(9, 0),
                active_end=time(9, 0),
            )

    def test_wrap_around_time_window_allowed(self):
        s = SlideIn(
            source_asset_id=SID,
            kind="asset",
            active_start=time(22, 0),
            active_end=time(2, 0),
        )
        assert s.active_start == time(22, 0)
        assert s.active_end == time(2, 0)

    def test_iso_string_times_and_dates_parse(self):
        s = SlideIn(
            source_asset_id=SID,
            kind="asset",
            valid_from="2026-12-01",
            valid_to="2026-12-26",
            active_start="13:00",
            active_end="14:00",
        )
        assert s.valid_from == date(2026, 12, 1)
        assert s.active_start == time(13, 0)


# ---------------------------------------------------------------------------
# _load_slide_specs — closed slides dropped (integration)
# ---------------------------------------------------------------------------

async def _seed_image(db, *, filename):
    a = Asset(
        filename=filename,
        asset_type=AssetType.IMAGE,
        size_bytes=100,
        checksum=f"sha-{filename}",
        is_global=True,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _seed_slideshow_with_windows(db, srcs_and_windows):
    """``srcs_and_windows`` is a list of ``(source_asset, window_kwargs)``."""
    ss = Asset(
        filename="vis-deck",
        asset_type=AssetType.SLIDESHOW,
        size_bytes=0,
        checksum="",
        is_global=True,
    )
    db.add(ss)
    await db.commit()
    await db.refresh(ss)
    for idx, (src, win) in enumerate(srcs_and_windows):
        db.add(SlideshowSlide(
            slideshow_asset_id=ss.id,
            source_asset_id=src.id,
            position=idx,
            duration_ms=5000,
            play_to_end=False,
            **win,
        ))
    await db.commit()
    await db.refresh(ss)
    return ss


@pytest.mark.asyncio
class TestLoadSlideSpecsWindowing:
    async def test_none_local_now_shows_all_slides(self, db_session):
        a = await _seed_image(db_session, filename="open.png")
        b = await _seed_image(db_session, filename="future.png")
        ss = await _seed_slideshow_with_windows(db_session, [
            (a, {}),
            (b, {"valid_from": date(2099, 1, 1)}),  # far future
        ])
        # local_now=None -> builder/readiness path -> no filtering.
        specs = await _load_slide_specs(ss, db_session, None)
        assert {s.source_asset_id for s in specs} == {a.id, b.id}

    async def test_closed_slide_dropped_when_local_now_supplied(self, db_session):
        a = await _seed_image(db_session, filename="always.png")
        b = await _seed_image(db_session, filename="xmas.png")
        ss = await _seed_slideshow_with_windows(db_session, [
            (a, {}),
            (b, {"valid_from": date(2026, 12, 1), "valid_to": date(2026, 12, 26)}),
        ])
        # A date outside the December window: only the always-on slide survives.
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        specs = await _load_slide_specs(ss, db_session, now)
        assert [s.source_asset_id for s in specs] == [a.id]

    async def test_open_slide_kept_when_local_now_in_window(self, db_session):
        b = await _seed_image(db_session, filename="xmas.png")
        ss = await _seed_slideshow_with_windows(db_session, [
            (b, {"valid_from": date(2026, 12, 1), "valid_to": date(2026, 12, 26)}),
        ])
        now = datetime(2026, 12, 10, 12, 0, tzinfo=timezone.utc)
        specs = await _load_slide_specs(ss, db_session, now)
        assert [s.source_asset_id for s in specs] == [b.id]

    async def test_whole_deck_closed_yields_empty_specs(self, db_session):
        a = await _seed_image(db_session, filename="only.png")
        ss = await _seed_slideshow_with_windows(db_session, [
            (a, {"active_start": time(13, 0), "active_end": time(14, 0)}),
        ])
        # 09:00 is outside the 1-2pm window -> deck resolves empty.
        now = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
        specs = await _load_slide_specs(ss, db_session, now)
        assert specs == []


@pytest.mark.asyncio
class TestLoadSlideSpecsEmitWindows:
    """``emit_windows=True`` (the capability path) keeps closed slides and
    carries their typed window columns onto the spec so the device evaluates
    the window itself."""

    async def test_closed_slide_kept_and_window_carried(self, db_session):
        a = await _seed_image(db_session, filename="always2.png")
        b = await _seed_image(db_session, filename="xmas2.png")
        win = {"valid_from": date(2026, 12, 1), "valid_to": date(2026, 12, 26)}
        ss = await _seed_slideshow_with_windows(db_session, [(a, {}), (b, win)])
        # June is outside the December window, but emit_windows keeps it.
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        specs = await _load_slide_specs(ss, db_session, now, emit_windows=True)
        assert {s.source_asset_id for s in specs} == {a.id, b.id}
        bspec = next(s for s in specs if s.source_asset_id == b.id)
        assert bspec.valid_from == date(2026, 12, 1)
        assert bspec.valid_to == date(2026, 12, 26)
        # The unwindowed slide carries all-None window fields.
        aspec = next(s for s in specs if s.source_asset_id == a.id)
        assert aspec.valid_from is None and aspec.valid_to is None

    async def test_active_days_normalised_to_none_when_empty(self, db_session):
        a = await _seed_image(db_session, filename="daily.png")
        ss = await _seed_slideshow_with_windows(db_session, [(a, {})])
        now = datetime(2026, 6, 15, 12, 0, tzinfo=timezone.utc)
        specs = await _load_slide_specs(ss, db_session, now, emit_windows=True)
        assert specs[0].active_days is None


class TestResolvedChecksumWindows:
    """Pure-function tests for the ``emit_windows`` checksum fold.

    These pin the R1 regression guarantee: the non-capability digest is
    byte-identical regardless of window definitions (no spurious fleet
    re-push), while the capability digest folds the time-invariant window
    definition (author edits → exactly one re-push, stable across ticks).
    """

    def _plan(self, pos, *, checksum, **win):
        from cms.models.asset import AssetType as _AT
        from cms.services.slideshow_resolver import _SlidePlan
        return _SlidePlan(
            position=pos,
            source_asset_id=uuid4(),
            source_filename=f"s{pos}.png",
            source_asset_type=_AT.IMAGE,
            duration_ms=5000,
            play_to_end=False,
            checksum=checksum,
            **win,
        )

    def test_noncap_digest_ignores_window_definitions(self):
        from cms.services.slideshow_resolver import (
            _compute_resolved_manifest_checksum as cs,
        )
        plain = self._plan(0, checksum="x")
        windowed = self._plan(
            0, checksum="x", valid_from=date(2026, 12, 1), valid_to=date(2026, 12, 26)
        )
        # Same source id so the only difference is the window definition.
        windowed.source_asset_id = plain.source_asset_id
        windowed.source_filename = plain.source_filename
        assert cs("a", [plain], False, False) == cs("a", [windowed], False, False)

    def test_cap_digest_folds_window_definition(self):
        from cms.services.slideshow_resolver import (
            _compute_resolved_manifest_checksum as cs,
        )
        plain = self._plan(0, checksum="x")
        windowed = self._plan(0, checksum="x", active_start=time(13, 0), active_end=time(14, 0))
        windowed.source_asset_id = plain.source_asset_id
        windowed.source_filename = plain.source_filename
        # On the capability path the window is folded → different digest.
        assert cs("a", [plain], False, True) != cs("a", [windowed], False, True)

    def test_cap_digest_stable_across_ticks(self):
        from cms.services.slideshow_resolver import (
            _compute_resolved_manifest_checksum as cs,
        )
        p = self._plan(0, checksum="x", valid_from=date(2026, 12, 1))
        # Two folds of the identical time-invariant window → identical digest.
        assert cs("a", [p], False, True) == cs("a", [p], False, True)

    def test_cap_digest_differs_from_noncap_when_windowed(self):
        from cms.services.slideshow_resolver import (
            _compute_resolved_manifest_checksum as cs,
        )
        p = self._plan(0, checksum="x", valid_from=date(2026, 12, 1))
        assert cs("a", [p], False, True) != cs("a", [p], False, False)


# ---------------------------------------------------------------------------
# expired_slide_counts — library "will never show again" warning helper
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
class TestExpiredSlideCounts:
    """A slide counts as permanently expired only when ``valid_to`` is set and
    strictly before the supplied ``today_local`` — the library badge basis."""

    TODAY = date(2026, 6, 15)

    async def test_past_valid_to_counts(self, db_session):
        from cms.services.slideshow_resolver import expired_slide_counts

        a = await _seed_image(db_session, filename="exp-past.png")
        ss = await _seed_slideshow_with_windows(
            db_session, [(a, {"valid_to": date(2026, 6, 14)})]
        )
        counts = await expired_slide_counts([ss.id], self.TODAY, db_session)
        assert counts[ss.id] == 1

    async def test_today_valid_to_not_expired(self, db_session):
        """``valid_to == today`` is still showing today — not expired."""
        from cms.services.slideshow_resolver import expired_slide_counts

        a = await _seed_image(db_session, filename="exp-today.png")
        ss = await _seed_slideshow_with_windows(
            db_session, [(a, {"valid_to": self.TODAY})]
        )
        counts = await expired_slide_counts([ss.id], self.TODAY, db_session)
        assert counts[ss.id] == 0

    async def test_future_and_null_valid_to_not_expired(self, db_session):
        from cms.services.slideshow_resolver import expired_slide_counts

        future = await _seed_image(db_session, filename="exp-future.png")
        recurring = await _seed_image(db_session, filename="exp-recurring.png")
        ss = await _seed_slideshow_with_windows(
            db_session,
            [
                (future, {"valid_to": date(2026, 12, 25)}),
                # No valid_to, only a weekday window → recurs forever.
                (recurring, {"active_days": [0, 1, 2]}),
            ],
        )
        counts = await expired_slide_counts([ss.id], self.TODAY, db_session)
        assert counts[ss.id] == 0

    async def test_multiple_expired_sum(self, db_session):
        from cms.services.slideshow_resolver import expired_slide_counts

        a = await _seed_image(db_session, filename="exp-m1.png")
        b = await _seed_image(db_session, filename="exp-m2.png")
        c = await _seed_image(db_session, filename="exp-m3.png")
        ss = await _seed_slideshow_with_windows(
            db_session,
            [
                (a, {"valid_to": date(2025, 1, 1)}),
                (b, {"valid_from": date(2026, 1, 1), "valid_to": date(2026, 6, 1)}),
                (c, {}),  # always-on, not expired
            ],
        )
        counts = await expired_slide_counts([ss.id], self.TODAY, db_session)
        assert counts[ss.id] == 2

    async def test_empty_input_returns_empty_mapping(self, db_session):
        from cms.services.slideshow_resolver import expired_slide_counts

        assert await expired_slide_counts([], self.TODAY, db_session) == {}