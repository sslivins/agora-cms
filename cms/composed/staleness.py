"""Stale-bundle detection for composed slides.

The bundle builder records the union of every widget's declared asset
dependencies in ``ComposedSlide.bundle_source_asset_ids`` at build
time (see ``cms/composed/bundle.py``).  Once a slide is published,
the user can continue editing the draft layout — adding widgets,
removing them, swapping referenced assets — and the published bundle
gradually drifts out of sync with what the layout now says.

This module exposes :func:`compute_staleness` so the editor can
surface "your bundle is stale, rebuild?" affordances and the publish
endpoint can skip a no-op rebuild when the bundle is already fresh.

What this catches (v1):
    * Bundle was never built (``bundle_built_at`` is None).
    * The layout now references asset IDs the bundle doesn't have
      ("added") — e.g. user dropped a new image widget after publish.
    * The bundle references asset IDs the layout no longer mentions
      ("removed") — e.g. user deleted a widget after publish.

What this does NOT catch (v1):
    * Same asset ID, different bytes (user re-uploaded an image in
      place).  Detecting this requires recording per-asset checksums
      at build time, which means a schema bump.  Tracked as a
      follow-up; for now editors should treat "I changed the source
      file" as a manual-rebuild trigger.

This service is intentionally read-only and side-effect-free: callers
choose whether to act on the report (show a banner, gate a publish,
auto-trigger a rebuild, etc.).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError as PydanticValidationError

from cms.composed.registry import WidgetRegistry
from cms.composed.schema import Layout

StalenessReason = Literal[
    "fresh",
    "never_built",
    "asset_ids_drift",
    "layout_unloadable",
]


@dataclass
class StaleBundleReport:
    """Result of :func:`compute_staleness`.

    ``is_stale`` is the single-bit answer the UI cares about; the
    other fields explain *why* so the editor can render a useful
    message and tests can assert specific drift modes.
    """

    is_stale: bool
    reason: StalenessReason
    added: set[uuid.UUID] = field(default_factory=set)
    removed: set[uuid.UUID] = field(default_factory=set)
    # Set when reason == "layout_unloadable": the Pydantic error
    # message.  Callers usually surface this as "layout itself is
    # broken; fix that before worrying about staleness".
    layout_error: str | None = None


def _compute_current_asset_ids(
    layout: Layout, registry: WidgetRegistry
) -> set[uuid.UUID]:
    """Re-derive the union of ``declared_asset_ids`` over every widget
    instance in ``layout``.  Mirrors the loop the bundle builder runs
    at build time (see ``bundle.py``), minus the rendering side.

    Unknown widget slugs and per-widget config-validation failures are
    swallowed silently here: the dedicated layout validator
    (``validate_layout``) is the right place to surface those.  A
    staleness check whose only "issue" is that a config doesn't
    validate cleanly should still report on whatever IS resolvable.
    """
    current: set[uuid.UUID] = set()
    for inst in layout.widgets:
        widget = registry.get(inst.type)
        if widget is None:
            continue
        try:
            typed = widget.ConfigSchema.model_validate(inst.config)
        except PydanticValidationError:
            continue
        current.update(widget.declared_asset_ids(typed))
    return current


def compute_staleness(
    slide,  # ComposedSlide ORM row (not imported to keep this module test-friendly)
    registry: WidgetRegistry,
) -> StaleBundleReport:
    """Compare the slide's current layout against its last bundle.

    Returns a :class:`StaleBundleReport`.  Does not touch the database
    and does not mutate ``slide``.
    """
    if slide.bundle_built_at is None:
        return StaleBundleReport(is_stale=True, reason="never_built")

    layout_json = slide.layout_json or {}
    try:
        layout = Layout.model_validate(layout_json)
    except PydanticValidationError as e:
        # Defensive: a previously-published slide whose draft layout
        # has since been corrupted (or hand-edited via the DB) can
        # still hit staleness checks from the editor.  Treat that as
        # "needs attention" so callers don't quietly mark it fresh.
        return StaleBundleReport(
            is_stale=True,
            reason="layout_unloadable",
            layout_error=str(e),
        )

    current_ids = _compute_current_asset_ids(layout, registry)
    bundle_ids = set(slide.bundle_source_asset_ids or [])
    added = current_ids - bundle_ids
    removed = bundle_ids - current_ids

    if added or removed:
        return StaleBundleReport(
            is_stale=True,
            reason="asset_ids_drift",
            added=added,
            removed=removed,
        )

    return StaleBundleReport(is_stale=False, reason="fresh")
