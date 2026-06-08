"""Tests for cms.composed.widgets.shape.ShapeWidget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import cms.composed.widgets  # noqa: F401
from cms.composed.registry import get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.shape import ShapeWidget, ShapeWidgetConfig


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


class TestShapeWidgetConfig:
    def test_defaults(self):
        c = ShapeWidgetConfig()
        assert c.shape == "rectangle"
        assert c.fill == "#3b82f6"
        assert c.border_width == 0
        assert c.border_color == "#000000"
        assert c.corner_radius == 0
        assert c.thickness == 8
        assert c.orientation == "horizontal"

    def test_shape_allowlist(self):
        for ok in ("rectangle", "ellipse", "line"):
            ShapeWidgetConfig(shape=ok)
        with pytest.raises(ValidationError):
            ShapeWidgetConfig(shape="triangle")  # type: ignore[arg-type]

    def test_orientation_allowlist(self):
        for ok in ("horizontal", "vertical"):
            ShapeWidgetConfig(shape="line", orientation=ok)
        with pytest.raises(ValidationError):
            ShapeWidgetConfig(orientation="diagonal")  # type: ignore[arg-type]

    def test_fill_must_be_hex_6(self):
        with pytest.raises(ValidationError):
            ShapeWidgetConfig(fill="blue")

    def test_border_color_must_be_hex_6(self):
        with pytest.raises(ValidationError):
            ShapeWidgetConfig(border_color="black")

    def test_border_width_bounds(self):
        ShapeWidgetConfig(border_width=0)
        ShapeWidgetConfig(border_width=100)
        with pytest.raises(ValidationError):
            ShapeWidgetConfig(border_width=-1)
        with pytest.raises(ValidationError):
            ShapeWidgetConfig(border_width=101)

    def test_corner_radius_bounds(self):
        ShapeWidgetConfig(corner_radius=0)
        ShapeWidgetConfig(corner_radius=500)
        with pytest.raises(ValidationError):
            ShapeWidgetConfig(corner_radius=501)

    def test_thickness_bounds(self):
        ShapeWidgetConfig(thickness=1)
        ShapeWidgetConfig(thickness=500)
        with pytest.raises(ValidationError):
            ShapeWidgetConfig(thickness=0)
        with pytest.raises(ValidationError):
            ShapeWidgetConfig(thickness=501)

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ShapeWidgetConfig(rotation=45)  # type: ignore[call-arg]


class TestShapeWidgetRegistry:
    def test_registered(self):
        w = get_registry().get("shape")
        assert isinstance(w, ShapeWidget)
        assert w.slug == "shape"

    def test_default_config_validates(self):
        w = ShapeWidget()
        ShapeWidgetConfig(**w.default_config())

    def test_not_assistant_hidden(self):
        # The assistant should be able to place shapes.
        assert get_registry().get("shape").assistant_hidden is False


class TestShapeWidgetRender:
    def test_no_declared_assets(self):
        w = ShapeWidget()
        assert w.declared_asset_ids(ShapeWidgetConfig()) == []

    def test_rectangle_fill(self):
        w = ShapeWidget()
        r = w.render_html(ShapeWidgetConfig(fill="#112233"), _cell(), "abc")
        assert "background: #112233" in r.css
        assert "border-radius" not in r.css  # no rounding by default

    def test_rectangle_corner_radius(self):
        w = ShapeWidget()
        r = w.render_html(
            ShapeWidgetConfig(shape="rectangle", corner_radius=24),
            _cell(),
            "abc",
        )
        assert "border-radius: 24px;" in r.css

    def test_rectangle_border(self):
        w = ShapeWidget()
        r = w.render_html(
            ShapeWidgetConfig(border_width=5, border_color="#abcdef"),
            _cell(),
            "abc",
        )
        assert "border: 5px solid #abcdef;" in r.css

    def test_ellipse_is_round(self):
        w = ShapeWidget()
        r = w.render_html(ShapeWidgetConfig(shape="ellipse"), _cell(), "abc")
        assert "border-radius: 50%;" in r.css

    def test_ellipse_ignores_corner_radius(self):
        w = ShapeWidget()
        r = w.render_html(
            ShapeWidgetConfig(shape="ellipse", corner_radius=40),
            _cell(),
            "abc",
        )
        assert "border-radius: 50%;" in r.css
        assert "40px" not in r.css

    def test_line_horizontal(self):
        w = ShapeWidget()
        r = w.render_html(
            ShapeWidgetConfig(shape="line", thickness=12, fill="#ff0000"),
            _cell(),
            "abc",
        )
        assert "height: 12px;" in r.css
        assert "width: 100%;" in r.css
        assert "background: #ff0000;" in r.css
        assert "-rule" in r.html

    def test_line_vertical(self):
        w = ShapeWidget()
        r = w.render_html(
            ShapeWidgetConfig(
                shape="line", orientation="vertical", thickness=6
            ),
            _cell(),
            "abc",
        )
        assert "width: 6px;" in r.css
        assert "height: 100%;" in r.css

    def test_line_endcap_radius(self):
        w = ShapeWidget()
        r = w.render_html(
            ShapeWidgetConfig(shape="line", corner_radius=4),
            _cell(),
            "abc",
        )
        assert "border-radius: 4px;" in r.css

    def test_no_init_js_or_assets(self):
        w = ShapeWidget()
        r = w.render_html(ShapeWidgetConfig(), _cell(), "abc")
        assert r.init_js is None
        assert r.js == ""
        assert r.static_assets == []
        assert r.referenced_asset_ids == []

    def test_no_external_references(self):
        w = ShapeWidget()
        r = w.render_html(ShapeWidgetConfig(), _cell(), "abc")
        assert "src=" not in r.html
        assert "href=" not in r.html
        assert "<script" not in r.html

    def test_html_and_css_are_instance_scoped(self):
        w = ShapeWidget()
        cfg = ShapeWidgetConfig()
        r1 = w.render_html(cfg, _cell(), "inst-A")
        r2 = w.render_html(cfg, _cell(), "inst-B")
        assert "cw-shape-inst-A" in r1.html
        assert "cw-shape-inst-A" in r1.css
        assert "cw-shape-inst-B" in r2.html
        assert "cw-shape-inst-B" not in r1.html
        assert "cw-shape-inst-A" not in r2.html

    def test_render_is_deterministic(self):
        w = ShapeWidget()
        cfg = ShapeWidgetConfig(shape="ellipse", fill="#0a0b0c")
        r1 = w.render_html(cfg, _cell(), "abc")
        r2 = w.render_html(cfg, _cell(), "abc")
        assert r1.html == r2.html
        assert r1.css == r2.css

    def test_validate_semantic_ok(self):
        w = ShapeWidget()
        assert w.validate_semantic(ShapeWidgetConfig()) == []
