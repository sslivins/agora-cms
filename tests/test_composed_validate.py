"""Phase 0 tests for composed-slide semantic validator."""

from __future__ import annotations

import uuid

from pydantic import BaseModel, ConfigDict, Field

from cms.composed.registry import (
    Widget,
    WidgetRegistry,
    WidgetRender,
)
from cms.composed.schema import (
    GRID_COLS,
    GRID_ROWS,
    Cell,
    Layout,
    WidgetInstance,
)
from cms.composed.validate import validate_layout


# ── Stub widget for tests ────────────────────────────────────────────


class _StubConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: str = Field(..., min_length=1)


class _SemanticConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    asset_id: uuid.UUID


class _StubWidget(Widget):
    slug = "stub"
    display_name = "Stub"
    icon = "stub-icon"
    ConfigSchema = _StubConfig

    def render_html(self, config, cell, instance_id):
        return WidgetRender(html=f"<div>{config.text}</div>")

    def editor_template(self):
        return "stub.html"

    def default_config(self):
        return {"text": "hi"}


class _SemanticWidget(Widget):
    """Widget whose semantic hook rejects a known sentinel asset."""

    slug = "semantic"
    display_name = "Semantic"
    icon = "semantic-icon"
    ConfigSchema = _SemanticConfig

    BAD = uuid.UUID("00000000-0000-0000-0000-deadbeefdead")

    def render_html(self, config, cell, instance_id):
        return WidgetRender(html="<div>semantic</div>")

    def editor_template(self):
        return "semantic.html"

    def default_config(self):
        return {"asset_id": str(uuid.uuid4())}

    def validate_semantic(self, config):
        if config.asset_id == self.BAD:
            return ["asset not found"]
        return []


def _registry() -> WidgetRegistry:
    reg = WidgetRegistry()
    reg.register(_StubWidget())
    reg.register(_SemanticWidget())
    return reg


def _wi(*, type="stub", cell=None, config=None, wid=None) -> WidgetInstance:
    return WidgetInstance(
        id=wid or uuid.uuid4(),
        type=type,
        cell=cell or Cell(row=1, col=1),
        config=config if config is not None else {"text": "hi"},
    )


# ── Valid layouts ────────────────────────────────────────────────────


def test_valid_layout_returns_no_errors():
    layout = Layout(
        widgets=[
            _wi(cell=Cell(row=1, col=1, colspan=4)),
            _wi(cell=Cell(row=1, col=5, colspan=8)),
            _wi(cell=Cell(row=2, col=1, rowspan=GRID_ROWS - 1, colspan=GRID_COLS)),
        ]
    )
    assert validate_layout(layout, _registry()) == []


def test_empty_layout_is_valid():
    assert validate_layout(Layout(), _registry()) == []


# ── Cell bounds (rowspan/colspan beyond grid) ────────────────────────


def test_rowspan_extending_past_grid_is_rejected():
    layout = Layout(
        widgets=[
            _wi(cell=Cell(row=GRID_ROWS, col=1, rowspan=2)),
        ]
    )
    errors = validate_layout(layout, _registry())
    codes = [e.code for e in errors]
    assert "cell_out_of_bounds" in codes


def test_colspan_extending_past_grid_is_rejected():
    layout = Layout(
        widgets=[
            _wi(cell=Cell(row=1, col=GRID_COLS, colspan=2)),
        ]
    )
    errors = validate_layout(layout, _registry())
    assert any(e.code == "cell_out_of_bounds" for e in errors)


# ── Overlap detection ────────────────────────────────────────────────


def test_overlap_reports_both_widget_ids():
    a = uuid.uuid4()
    b = uuid.uuid4()
    layout = Layout(
        widgets=[
            _wi(wid=a, cell=Cell(row=1, col=1, rowspan=2, colspan=2)),
            _wi(wid=b, cell=Cell(row=2, col=2, rowspan=2, colspan=2)),
        ]
    )
    errors = [e for e in validate_layout(layout, _registry()) if e.code == "cells_overlap"]
    assert errors, "expected cells_overlap"
    msg = errors[0].message
    assert str(a) in msg
    assert str(b) in msg


def test_adjacent_widgets_do_not_overlap():
    layout = Layout(
        widgets=[
            _wi(cell=Cell(row=1, col=1, colspan=6)),
            _wi(cell=Cell(row=1, col=7, colspan=6)),
        ]
    )
    assert validate_layout(layout, _registry()) == []


# ── Duplicate IDs ────────────────────────────────────────────────────


def test_duplicate_widget_ids_are_rejected():
    shared = uuid.uuid4()
    layout = Layout(
        widgets=[
            _wi(wid=shared, cell=Cell(row=1, col=1)),
            _wi(wid=shared, cell=Cell(row=1, col=2)),
        ]
    )
    errors = validate_layout(layout, _registry())
    assert any(e.code == "duplicate_widget_id" for e in errors)


# ── Unknown widget type ──────────────────────────────────────────────


def test_unknown_widget_type_is_rejected():
    layout = Layout(
        widgets=[
            WidgetInstance(
                id=uuid.uuid4(),
                type="not_a_real_widget",
                cell=Cell(row=1, col=1),
                config={},
            )
        ]
    )
    errors = validate_layout(layout, _registry())
    assert any(e.code == "unknown_widget_type" for e in errors)


# ── Per-widget config validation ─────────────────────────────────────


def test_widget_config_shape_violation_reported():
    layout = Layout(
        widgets=[
            _wi(config={"text": ""}),  # min_length=1 fails
        ]
    )
    errors = validate_layout(layout, _registry())
    assert any(e.code == "widget_config_shape" for e in errors)


def test_widget_config_unknown_field_reported():
    layout = Layout(
        widgets=[
            _wi(config={"text": "ok", "rogue": True}),  # extra=forbid
        ]
    )
    errors = validate_layout(layout, _registry())
    assert any(e.code == "widget_config_shape" for e in errors)


def test_widget_semantic_failure_reported():
    layout = Layout(
        widgets=[
            WidgetInstance(
                id=uuid.uuid4(),
                type="semantic",
                cell=Cell(row=1, col=1),
                config={"asset_id": str(_SemanticWidget.BAD)},
            )
        ]
    )
    errors = validate_layout(layout, _registry())
    assert any(e.code == "widget_config_semantic" for e in errors)


def test_widget_semantic_passes_for_good_asset():
    layout = Layout(
        widgets=[
            WidgetInstance(
                id=uuid.uuid4(),
                type="semantic",
                cell=Cell(row=1, col=1),
                config={"asset_id": str(uuid.uuid4())},
            )
        ]
    )
    assert validate_layout(layout, _registry()) == []


# ── Registry behaviour ───────────────────────────────────────────────


def test_registry_rejects_duplicate_slug():
    reg = WidgetRegistry()
    reg.register(_StubWidget())
    import pytest as _pt

    with _pt.raises(ValueError):
        reg.register(_StubWidget())


def test_registry_lookup_helpers():
    reg = _registry()
    assert reg.has("stub")
    assert not reg.has("nope")
    assert reg.get("stub") is not None
    assert reg.get("nope") is None
    assert "stub" in reg.slugs()
    assert "semantic" in reg.slugs()
