"""Tests for ``cms.composed.staleness.compute_staleness``.

The staleness service compares a slide's current draft layout against
the asset IDs the last bundle was built from.  It's pure / read-only,
so we can exercise it with a hand-rolled ``SimpleNamespace`` slide
stub instead of standing up real ORM rows — the function never
touches the session.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

# Side-effect import: registers built-in widgets (text, image, ...)
import cms.composed.widgets  # noqa: F401
from cms.composed.registry import get_registry
from cms.composed.staleness import (
    StaleBundleReport,
    compute_staleness,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _layout_with_text(text: str = "hello") -> dict:
    """A minimal valid layout containing a single text widget (no
    asset deps).  Used when we want a layout that's loadable but
    declares zero asset IDs.
    """
    return {
        "schema_version": 1,
        "background": {"color": "#000000"},
        "widgets": [
            {
                "id": str(uuid.uuid4()),
                "type": "text",
                "cell": {"row": 1, "col": 1, "rowspan": 1, "colspan": 1},
                "config_version": 1,
                "config": {
                    "text": text,
                    "color": "#ffffff",
                    "font_size_px": 48,
                    "font_family": "sans-serif",
                },
            },
        ],
    }


def _layout_with_image(asset_id: uuid.UUID) -> dict:
    """A minimal valid layout containing a single image widget that
    declares ``asset_id`` as its dependency.
    """
    return {
        "schema_version": 1,
        "background": {"color": "#000000"},
        "widgets": [
            {
                "id": str(uuid.uuid4()),
                "type": "image",
                "cell": {"row": 1, "col": 1, "rowspan": 2, "colspan": 2},
                "config_version": 1,
                "config": {"asset_id": str(asset_id), "object_fit": "contain"},
            },
        ],
    }


def _layout_with_images(*asset_ids: uuid.UUID) -> dict:
    """Layout containing N image widgets, one per asset ID."""
    widgets = []
    for i, aid in enumerate(asset_ids):
        widgets.append(
            {
                "id": str(uuid.uuid4()),
                "type": "image",
                "cell": {"row": 1 + i, "col": 1, "rowspan": 1, "colspan": 1},
                "config_version": 1,
                "config": {"asset_id": str(aid), "object_fit": "contain"},
            }
        )
    return {
        "schema_version": 1,
        "background": {"color": "#000000"},
        "widgets": widgets,
    }


def _slide(
    *,
    layout_json: dict | None = None,
    bundle_built_at: datetime | None = None,
    bundle_source_asset_ids: list[uuid.UUID] | None = None,
) -> SimpleNamespace:
    """Build a minimal slide-shaped object for the staleness function.

    ``compute_staleness`` only reads ``bundle_built_at``,
    ``bundle_source_asset_ids``, and ``layout_json`` — we use
    SimpleNamespace so the test doesn't depend on the SQLAlchemy
    model surface.
    """
    return SimpleNamespace(
        layout_json=layout_json or _layout_with_text(),
        bundle_built_at=bundle_built_at,
        bundle_source_asset_ids=bundle_source_asset_ids,
    )


# ---------------------------------------------------------------------
# never_built path
# ---------------------------------------------------------------------


class TestNeverBuilt:
    def test_no_bundle_built_yet_is_stale(self):
        registry = get_registry()
        slide = _slide(bundle_built_at=None)
        report = compute_staleness(slide, registry)
        assert report.is_stale is True
        assert report.reason == "never_built"
        assert report.added == set()
        assert report.removed == set()

    def test_never_built_short_circuits_before_layout_parse(self):
        """A slide that's never been built returns ``never_built``
        even if its layout would otherwise fail to load.  This keeps
        the message accurate for the editor — telling someone their
        layout is broken when the real issue is "you haven't hit
        publish yet" would be confusing.
        """
        registry = get_registry()
        slide = _slide(layout_json={"not": "valid"}, bundle_built_at=None)
        report = compute_staleness(slide, registry)
        assert report.reason == "never_built"


# ---------------------------------------------------------------------
# fresh path
# ---------------------------------------------------------------------


class TestFresh:
    def test_no_asset_widgets_no_bundle_ids_is_fresh(self):
        registry = get_registry()
        slide = _slide(
            layout_json=_layout_with_text(),
            bundle_built_at=datetime.now(timezone.utc),
            bundle_source_asset_ids=[],
        )
        report = compute_staleness(slide, registry)
        assert report.is_stale is False
        assert report.reason == "fresh"

    def test_no_asset_widgets_none_bundle_ids_is_fresh(self):
        """``bundle_source_asset_ids`` is nullable in the schema; the
        bundle builder leaves it None for slides without asset deps.
        Treat None and [] equivalently.
        """
        registry = get_registry()
        slide = _slide(
            layout_json=_layout_with_text(),
            bundle_built_at=datetime.now(timezone.utc),
            bundle_source_asset_ids=None,
        )
        report = compute_staleness(slide, registry)
        assert report.reason == "fresh"

    def test_matching_single_asset_id_is_fresh(self):
        registry = get_registry()
        aid = uuid.uuid4()
        slide = _slide(
            layout_json=_layout_with_image(aid),
            bundle_built_at=datetime.now(timezone.utc),
            bundle_source_asset_ids=[aid],
        )
        report = compute_staleness(slide, registry)
        assert report.is_stale is False
        assert report.reason == "fresh"

    def test_matching_multi_asset_ids_is_fresh(self):
        registry = get_registry()
        aids = [uuid.uuid4() for _ in range(3)]
        slide = _slide(
            layout_json=_layout_with_images(*aids),
            bundle_built_at=datetime.now(timezone.utc),
            # Different ordering than the layout's — set semantics.
            bundle_source_asset_ids=list(reversed(aids)),
        )
        report = compute_staleness(slide, registry)
        assert report.reason == "fresh"


# ---------------------------------------------------------------------
# asset_ids_drift path
# ---------------------------------------------------------------------


class TestAssetIdsDrift:
    def test_added_asset_marks_stale(self):
        registry = get_registry()
        aid = uuid.uuid4()
        slide = _slide(
            layout_json=_layout_with_image(aid),
            bundle_built_at=datetime.now(timezone.utc),
            bundle_source_asset_ids=[],
        )
        report = compute_staleness(slide, registry)
        assert report.is_stale is True
        assert report.reason == "asset_ids_drift"
        assert report.added == {aid}
        assert report.removed == set()

    def test_removed_asset_marks_stale(self):
        registry = get_registry()
        old_aid = uuid.uuid4()
        slide = _slide(
            # Layout no longer references any assets…
            layout_json=_layout_with_text(),
            bundle_built_at=datetime.now(timezone.utc),
            # …but the bundle was built when it did.
            bundle_source_asset_ids=[old_aid],
        )
        report = compute_staleness(slide, registry)
        assert report.is_stale is True
        assert report.reason == "asset_ids_drift"
        assert report.added == set()
        assert report.removed == {old_aid}

    def test_swapped_asset_marks_both_added_and_removed(self):
        registry = get_registry()
        old_aid = uuid.uuid4()
        new_aid = uuid.uuid4()
        slide = _slide(
            layout_json=_layout_with_image(new_aid),
            bundle_built_at=datetime.now(timezone.utc),
            bundle_source_asset_ids=[old_aid],
        )
        report = compute_staleness(slide, registry)
        assert report.is_stale is True
        assert report.reason == "asset_ids_drift"
        assert report.added == {new_aid}
        assert report.removed == {old_aid}

    def test_partial_overlap_only_lists_drift(self):
        registry = get_registry()
        kept = uuid.uuid4()
        removed = uuid.uuid4()
        added = uuid.uuid4()
        slide = _slide(
            layout_json=_layout_with_images(kept, added),
            bundle_built_at=datetime.now(timezone.utc),
            bundle_source_asset_ids=[kept, removed],
        )
        report = compute_staleness(slide, registry)
        assert report.is_stale is True
        assert report.added == {added}
        assert report.removed == {removed}


# ---------------------------------------------------------------------
# layout_unloadable path
# ---------------------------------------------------------------------


class TestLayoutUnloadable:
    def test_corrupt_layout_reports_layout_error(self):
        """If a published slide's draft layout has been corrupted
        (e.g. via direct DB edit), staleness should report it rather
        than crash or silently claim fresh.
        """
        registry = get_registry()
        slide = _slide(
            layout_json={"schema_version": 1, "widgets": "not-a-list"},
            bundle_built_at=datetime.now(timezone.utc),
            bundle_source_asset_ids=[],
        )
        report = compute_staleness(slide, registry)
        assert report.is_stale is True
        assert report.reason == "layout_unloadable"
        assert report.layout_error is not None
        assert "widgets" in report.layout_error.lower()


# ---------------------------------------------------------------------
# Unknown widget / config-validation edge cases
# ---------------------------------------------------------------------


class TestUnknownWidget:
    def test_unknown_widget_type_does_not_crash(self):
        """If the layout references an unregistered widget type, the
        function should skip it for the staleness comparison — the
        layout validator owns that error.  We still want a usable
        report on the rest of the widgets.

        We construct this layout by bypassing Layout.model_validate
        (which the WidgetInstance type-validator would reject), and
        instead feed compute_staleness raw JSON via the model_dump
        path used by real callers.
        """
        registry = get_registry()
        known_aid = uuid.uuid4()
        # Hand-crafted layout JSON with one valid widget and one
        # unknown-typed widget.  Layout.model_validate accepts unknown
        # slugs at parse time (the slug validator only checks shape:
        # lowercase alphanumeric after stripping underscores); the
        # registry lookup is what rejects them.
        layout_json = {
            "schema_version": 1,
            "background": {"color": "#000000"},
            "widgets": [
                {
                    "id": str(uuid.uuid4()),
                    "type": "image",
                    "cell": {"row": 1, "col": 1, "rowspan": 1, "colspan": 1},
                    "config_version": 1,
                    "config": {
                        "asset_id": str(known_aid),
                        "object_fit": "contain",
                    },
                },
                {
                    "id": str(uuid.uuid4()),
                    "type": "totallymadeup",
                    "cell": {
                        "row": 2,
                        "col": 1,
                        "rowspan": 1,
                        "colspan": 1,
                    },
                    "config_version": 1,
                    "config": {"anything": "goes"},
                },
            ],
        }
        slide = _slide(
            layout_json=layout_json,
            bundle_built_at=datetime.now(timezone.utc),
            bundle_source_asset_ids=[known_aid],
        )
        report = compute_staleness(slide, registry)
        # Image widget's asset matches bundle; unknown widget skipped.
        assert report.is_stale is False
        assert report.reason == "fresh"

    def test_invalid_widget_config_does_not_crash(self):
        """A widget whose config fails Pydantic validation is silently
        skipped for staleness purposes — the layout validator surfaces
        that error elsewhere.
        """
        registry = get_registry()
        kept_aid = uuid.uuid4()
        layout_json = {
            "schema_version": 1,
            "background": {"color": "#000000"},
            "widgets": [
                {
                    "id": str(uuid.uuid4()),
                    "type": "image",
                    "cell": {"row": 1, "col": 1, "rowspan": 1, "colspan": 1},
                    "config_version": 1,
                    "config": {"asset_id": str(kept_aid), "object_fit": "contain"},
                },
                # Image widget with garbage config — model_validate will
                # raise inside compute_staleness; should be caught.
                {
                    "id": str(uuid.uuid4()),
                    "type": "image",
                    "cell": {"row": 2, "col": 1, "rowspan": 1, "colspan": 1},
                    "config_version": 1,
                    "config": {"asset_id": "not-a-uuid", "object_fit": "contain"},
                },
            ],
        }
        slide = _slide(
            layout_json=layout_json,
            bundle_built_at=datetime.now(timezone.utc),
            bundle_source_asset_ids=[kept_aid],
        )
        report = compute_staleness(slide, registry)
        assert report.is_stale is False
        assert report.reason == "fresh"


# ---------------------------------------------------------------------
# Dataclass behaviour
# ---------------------------------------------------------------------


class TestReportShape:
    def test_default_added_removed_are_distinct_sets(self):
        """Regression guard: ``set`` default args used to silently
        share state across instances back when dataclasses were less
        strict.  Use the explicit factory and verify."""
        a = StaleBundleReport(is_stale=False, reason="fresh")
        b = StaleBundleReport(is_stale=False, reason="fresh")
        a.added.add(uuid.uuid4())
        assert b.added == set()
