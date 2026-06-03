"""Pydantic schemas for the Composed Slide layout document.

The layout document is the single source of truth for a composed
slide's authoring state.  It is stored as-is in
``composed_slides.layout_json`` and is the input to both the live
preview endpoint and the offline bundle builder.

v1 constraints (locked in 2026-06-03, see ``plan.md``):

* Canvas is fixed at 1920×1080 (16:9).  Per-device profiles are
  deferred to a future major schema bump.
* Grid is fixed at 12 columns × 8 rows.  Snap-to-cell editor.
* No layering / z-index.  Cells must not overlap; the semantic
  validator (:mod:`cms.composed.validate`) enforces this.

The actual content of a widget's ``config`` dict is opaque at this
layer — each widget defines its own ``ConfigSchema``.  The
validator dispatches per-instance via the registry to validate
configs in their widget's terms.
"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field, field_validator


# ── Locked-in v1 constants ────────────────────────────────────────────
# These are intentionally not configurable in the schema.  Changing
# them is a breaking change that requires a ``schema_version`` bump
# and a per-widget migration story.

CANVAS_WIDTH = 1920
CANVAS_HEIGHT = 1080
GRID_COLS = 12
GRID_ROWS = 8
SCHEMA_VERSION = 1


class Cell(BaseModel):
    """A widget instance's placement on the snap grid.

    Coordinates are 1-indexed and inclusive on both ends (matching CSS
    grid-row / grid-column semantics).  ``row=1, col=1, rowspan=1,
    colspan=1`` is the top-left cell occupying exactly one grid cell.
    """

    model_config = ConfigDict(extra="forbid")

    row: int = Field(..., ge=1, le=GRID_ROWS)
    col: int = Field(..., ge=1, le=GRID_COLS)
    rowspan: int = Field(default=1, ge=1, le=GRID_ROWS)
    colspan: int = Field(default=1, ge=1, le=GRID_COLS)


class WidgetInstance(BaseModel):
    """One placed widget in a composed-slide layout.

    ``id`` is a stable UUID assigned by the editor on widget drop and
    persisted thereafter.  The bundle builder uses it to scope DOM
    IDs and CSS classes (see ``plan.md`` — Instance scoping rule).

    ``type`` is the widget slug (e.g. ``"text"``, ``"clock"``); it
    must match a widget registered in :mod:`cms.composed.registry`.
    The semantic validator rejects layouts that reference an unknown
    type.

    ``config`` is the widget-specific config dict, validated by the
    registered widget's ``ConfigSchema`` and ``validate_semantic``
    hook in :func:`cms.composed.validate.validate_layout`.

    ``config_version`` is recorded at save time from the widget's
    current ``config_version``.  On load, mismatched versions are
    upgraded via the widget's ``migrate_config`` hook and re-persisted.
    """

    model_config = ConfigDict(extra="forbid")

    id: uuid.UUID
    type: str = Field(..., min_length=1, max_length=64)
    cell: Cell
    config: dict = Field(default_factory=dict)
    config_version: int = Field(default=1, ge=1)

    @field_validator("type")
    @classmethod
    def _type_slug_shape(cls, v: str) -> str:
        # Slugs are lower-snake to match the on-disk widget module
        # filenames (``cms/composed/widgets/<slug>.py``) and to keep
        # them safe for use in CSS class names / DOM IDs.
        if not v.replace("_", "").isalnum() or not v.islower():
            raise ValueError(
                "widget type slug must be lowercase alphanumeric with optional underscores"
            )
        return v


class Background(BaseModel):
    """Slide-wide background settings.

    v1 supports a solid colour only.  Image / gradient backgrounds
    are deferred.
    """

    model_config = ConfigDict(extra="forbid")

    color: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")


class Grid(BaseModel):
    """Locked grid dimensions.

    Pinned in the schema so any layout JSON that disagrees is
    rejected at load time — protects us from old clients writing
    layouts with a different grid after a schema bump.
    """

    model_config = ConfigDict(extra="forbid")

    rows: int = Field(default=GRID_ROWS)
    cols: int = Field(default=GRID_COLS)

    @field_validator("rows")
    @classmethod
    def _rows_locked(cls, v: int) -> int:
        if v != GRID_ROWS:
            raise ValueError(f"grid.rows must be {GRID_ROWS} in schema v{SCHEMA_VERSION}")
        return v

    @field_validator("cols")
    @classmethod
    def _cols_locked(cls, v: int) -> int:
        if v != GRID_COLS:
            raise ValueError(f"grid.cols must be {GRID_COLS} in schema v{SCHEMA_VERSION}")
        return v


class Canvas(BaseModel):
    """Locked canvas pixel dimensions (1920×1080)."""

    model_config = ConfigDict(extra="forbid")

    width: int = Field(default=CANVAS_WIDTH)
    height: int = Field(default=CANVAS_HEIGHT)

    @field_validator("width")
    @classmethod
    def _w_locked(cls, v: int) -> int:
        if v != CANVAS_WIDTH:
            raise ValueError(f"canvas.width must be {CANVAS_WIDTH} in schema v{SCHEMA_VERSION}")
        return v

    @field_validator("height")
    @classmethod
    def _h_locked(cls, v: int) -> int:
        if v != CANVAS_HEIGHT:
            raise ValueError(f"canvas.height must be {CANVAS_HEIGHT} in schema v{SCHEMA_VERSION}")
        return v


class Layout(BaseModel):
    """Top-level composed-slide layout document."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=SCHEMA_VERSION)
    canvas: Canvas = Field(default_factory=Canvas)
    grid: Grid = Field(default_factory=Grid)
    background: Background = Field(default_factory=Background)
    widgets: list[WidgetInstance] = Field(default_factory=list)

    @field_validator("schema_version")
    @classmethod
    def _schema_version_supported(cls, v: int) -> int:
        # The Pydantic layer only accepts the *current* version.
        # Migration from older versions is handled in a separate
        # ``load_and_migrate`` path (not yet wired up — Phase 1B
        # introduces the first config-version bump worth migrating).
        if v != SCHEMA_VERSION:
            raise ValueError(
                f"unsupported layout schema_version {v}; current is {SCHEMA_VERSION}"
            )
        return v


def empty_layout() -> Layout:
    """Return a fresh empty layout for a brand-new composed slide."""
    return Layout()
