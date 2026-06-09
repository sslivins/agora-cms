"""Unit tests for the Date Banner composed-slide widget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cms.composed.registry import get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.datebanner import (
    DateBannerWidget,
    DateBannerWidgetConfig,
)


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


class TestDateBannerWidgetConfig:
    def test_defaults(self):
        c = DateBannerWidgetConfig()
        assert c.format == "full"
        assert c.prefix == ""
        assert c.uppercase is False
        assert c.color == "#ffffff"
        assert c.font_family == "sans"
        assert c.font_size_px == 96

    @pytest.mark.parametrize(
        "fmt", ["full", "long", "weekday", "short", "numeric"]
    )
    def test_valid_formats(self, fmt):
        assert DateBannerWidgetConfig(format=fmt).format == fmt

    def test_invalid_format_rejected(self):
        with pytest.raises(ValidationError):
            DateBannerWidgetConfig(format="fancy")

    def test_prefix_length_capped(self):
        with pytest.raises(ValidationError):
            DateBannerWidgetConfig(prefix="x" * 121)
        # exactly at the cap is fine
        assert DateBannerWidgetConfig(prefix="x" * 120).prefix == "x" * 120

    def test_font_allowlist(self):
        for f in ("sans", "serif", "mono"):
            assert DateBannerWidgetConfig(font_family=f).font_family == f
        with pytest.raises(ValidationError):
            DateBannerWidgetConfig(font_family="comic")

    def test_color_must_be_hex6(self):
        DateBannerWidgetConfig(color="#0aF3Cd")
        for bad in ("ffffff", "#fff", "#gggggg", "red"):
            with pytest.raises(ValidationError):
                DateBannerWidgetConfig(color=bad)

    def test_font_size_bounds(self):
        DateBannerWidgetConfig(font_size_px=8)
        DateBannerWidgetConfig(font_size_px=512)
        for bad in (7, 513, 0, -1):
            with pytest.raises(ValidationError):
                DateBannerWidgetConfig(font_size_px=bad)

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            DateBannerWidgetConfig(nope=1)


class TestDateBannerWidgetRegistry:
    def test_registered(self):
        reg = get_registry()
        assert reg.has("datebanner")
        w = reg.get("datebanner")
        assert isinstance(w, DateBannerWidget)
        assert w.display_name == "Date Banner"
        assert w.icon == "📅"

    def test_default_config_validates(self):
        w = DateBannerWidget()
        cfg = DateBannerWidgetConfig(**w.default_config())
        assert cfg.format == "full"


class TestDateBannerWidgetRender:
    def _render(self, **overrides):
        w = DateBannerWidget()
        cfg = DateBannerWidgetConfig(**{**w.default_config(), **overrides})
        return w.render_html(cfg, _cell(), "inst-1")

    def test_instance_scoped(self):
        r = self._render()
        assert "cw-datebanner-inst-1" in r.html
        assert "cw-datebanner-inst-1" in r.css
        assert "cw-datebanner-date-inst-1" in r.init_js

    def test_two_instances_dont_collide(self):
        w = DateBannerWidget()
        cfg = DateBannerWidgetConfig(**w.default_config())
        a = w.render_html(cfg, _cell(), "aaa")
        b = w.render_html(cfg, _cell(), "bbb")
        assert "cw-datebanner-aaa" in a.css
        assert "cw-datebanner-aaa" not in b.css
        assert "cw-datebanner-bbb" in b.css

    def test_init_js_has_setinterval_and_element_lookup(self):
        r = self._render()
        assert "getElementById('cw-datebanner-date-inst-1')" in r.init_js
        assert "setInterval(render, 60000)" in r.init_js
        assert "toLocaleDateString" in r.init_js

    @pytest.mark.parametrize(
        "fmt,needle",
        [
            ("full", "weekday:'long'"),
            ("long", "month:'long'"),
            ("weekday", "{weekday:'long'}"),
            ("short", "weekday:'short'"),
            ("numeric", "month:'2-digit'"),
        ],
    )
    def test_format_baked_into_options(self, fmt, needle):
        r = self._render(format=fmt)
        assert needle in r.init_js

    def test_prefix_injected_as_json_string(self):
        # A prefix containing a quote/backslash must be a safe JS literal,
        # not break out of the string.
        r = self._render(prefix='He said "hi"\\')
        assert 'var prefix = "He said \\"hi\\"\\\\";' in r.init_js

    def test_prefix_html_escaped_in_placeholder(self):
        r = self._render(prefix="<b>x</b>")
        assert "<b>x</b>" not in r.html
        assert "&lt;b&gt;x&lt;/b&gt;" in r.html

    def test_uppercase_flag_propagates(self):
        assert "var upper = true;" in self._render(uppercase=True).init_js
        assert "var upper = false;" in self._render(uppercase=False).init_js

    def test_color_and_size_in_css(self):
        r = self._render(color="#123456", font_size_px=144)
        assert "color: #123456;" in r.css
        assert "font-size: 144px;" in r.css

    def test_font_family_stack_in_css(self):
        assert "Georgia" in self._render(font_family="serif").css
        assert "ui-monospace" in self._render(font_family="mono").css

    def test_no_static_assets(self):
        r = self._render()
        assert r.static_assets == []
        assert r.referenced_asset_ids == []
