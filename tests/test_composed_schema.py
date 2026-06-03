"""Phase 0 tests for composed-slide Pydantic schemas."""

from __future__ import annotations

import json
import uuid

import pytest
from pydantic import ValidationError as PydanticValidationError

from cms.composed.schema import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    GRID_COLS,
    GRID_ROWS,
    SCHEMA_VERSION,
    Background,
    Canvas,
    Cell,
    Grid,
    Layout,
    WidgetInstance,
    empty_layout,
)


# ── Layout JSON round-trip ────────────────────────────────────────────


def test_empty_layout_has_locked_defaults():
    layout = empty_layout()
    assert layout.schema_version == SCHEMA_VERSION
    assert layout.canvas.width == CANVAS_WIDTH
    assert layout.canvas.height == CANVAS_HEIGHT
    assert layout.grid.rows == GRID_ROWS
    assert layout.grid.cols == GRID_COLS
    assert layout.widgets == []
    assert layout.background.color == "#000000"


def test_layout_round_trips_through_json():
    original = Layout(
        widgets=[
            WidgetInstance(
                id=uuid.uuid4(),
                type="text",
                cell=Cell(row=1, col=1, rowspan=2, colspan=3),
                config={"text": "hello"},
                config_version=1,
            )
        ],
        background=Background(color="#ff00ff"),
    )
    payload = original.model_dump_json()
    parsed = Layout.model_validate_json(payload)
    assert parsed == original

    # also via model_dump → dict → model_validate
    as_dict = json.loads(payload)
    parsed2 = Layout.model_validate(as_dict)
    assert parsed2 == original


# ── Locked dimensions rejected ────────────────────────────────────────


def test_canvas_rejects_non_locked_dimensions():
    with pytest.raises(PydanticValidationError):
        Canvas(width=1280, height=720)


def test_grid_rejects_non_locked_dimensions():
    with pytest.raises(PydanticValidationError):
        Grid(rows=4, cols=6)


def test_layout_rejects_unsupported_schema_version():
    with pytest.raises(PydanticValidationError):
        Layout(schema_version=999)


# ── Cell bounds (Pydantic-level) ──────────────────────────────────────


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(row=0, col=1),                # row < 1
        dict(row=1, col=0),                # col < 1
        dict(row=GRID_ROWS + 1, col=1),    # row > grid
        dict(row=1, col=GRID_COLS + 1),    # col > grid
        dict(row=1, col=1, rowspan=0),     # rowspan < 1
        dict(row=1, col=1, colspan=0),     # colspan < 1
    ],
)
def test_cell_rejects_bad_basic_values(kwargs):
    with pytest.raises(PydanticValidationError):
        Cell(**kwargs)


def test_cell_accepts_legitimate_values():
    Cell(row=1, col=1, rowspan=GRID_ROWS, colspan=GRID_COLS)


# ── Widget slug shape ────────────────────────────────────────────────


@pytest.mark.parametrize("slug", ["text", "scrolling_ticker", "clock24", "a1b2"])
def test_widget_type_accepts_valid_slugs(slug):
    WidgetInstance(
        id=uuid.uuid4(),
        type=slug,
        cell=Cell(row=1, col=1),
    )


@pytest.mark.parametrize(
    "slug",
    ["Text", "TEXT", "scrolling-ticker", "scrolling ticker", "ticker!", "", "with.dot"],
)
def test_widget_type_rejects_invalid_slugs(slug):
    with pytest.raises(PydanticValidationError):
        WidgetInstance(
            id=uuid.uuid4(),
            type=slug,
            cell=Cell(row=1, col=1),
        )


# ── extra="forbid" applies to all schemas ────────────────────────────


def test_layout_extra_field_rejected():
    with pytest.raises(PydanticValidationError):
        Layout.model_validate({"schema_version": SCHEMA_VERSION, "rogue": 1})


def test_cell_extra_field_rejected():
    with pytest.raises(PydanticValidationError):
        Cell.model_validate({"row": 1, "col": 1, "rogue": 2})


# ── Background colour shape ──────────────────────────────────────────


def test_background_rejects_non_hex_colour():
    with pytest.raises(PydanticValidationError):
        Background(color="red")
    with pytest.raises(PydanticValidationError):
        Background(color="#abc")  # 3-digit hex not supported in v1
