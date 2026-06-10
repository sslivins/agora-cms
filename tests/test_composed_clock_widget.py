"""Tests for cms.composed.widgets.clock.ClockWidget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import cms.composed.widgets  # noqa: F401
from cms.composed.registry import get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.clock import ClockWidget, ClockWidgetConfig


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


class TestClockWidgetConfig:
    def test_defaults(self):
        c = ClockWidgetConfig()
        assert c.format == "24h"
        assert c.show_seconds is True
        assert c.show_date is False
        assert c.color == "#ffffff"
        assert c.font_family == "sans"
        assert c.font_size_px == 96

    def test_format_allowlist(self):
        ClockWidgetConfig(format="12h")
        ClockWidgetConfig(format="24h")
        with pytest.raises(ValidationError):
            ClockWidgetConfig(format="48h")  # type: ignore[arg-type]

    def test_font_allowlist(self):
        for ok in ("sans", "serif", "mono"):
            ClockWidgetConfig(font_family=ok)
        with pytest.raises(ValidationError):
            ClockWidgetConfig(font_family="comic-sans")

    def test_color_must_be_hex_6(self):
        with pytest.raises(ValidationError):
            ClockWidgetConfig(color="red")

    def test_font_size_bounds(self):
        ClockWidgetConfig(font_size_px=8)
        ClockWidgetConfig(font_size_px=512)
        with pytest.raises(ValidationError):
            ClockWidgetConfig(font_size_px=7)
        with pytest.raises(ValidationError):
            ClockWidgetConfig(font_size_px=513)

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ClockWidgetConfig(timezone="UTC")  # type: ignore[call-arg]


class TestClockWidgetRegistry:
    def test_registered(self):
        w = get_registry().get("clock")
        assert isinstance(w, ClockWidget)
        assert w.slug == "clock"


class TestClockWidgetRender:
    def test_no_declared_assets(self):
        w = ClockWidget()
        cfg = ClockWidgetConfig()
        assert w.declared_asset_ids(cfg) == []

    def test_html_and_css_are_instance_scoped(self):
        w = ClockWidget()
        cfg = ClockWidgetConfig()

        r1 = w.render_html(cfg, _cell(), "inst-A")
        r2 = w.render_html(cfg, _cell(), "inst-B")

        assert "cw-clock-inst-A" in r1.html
        assert "cw-clock-inst-A" in r1.css
        assert "cw-clock-time-inst-A" in r1.html
        assert "cw-clock-inst-B" in r2.html
        # No cross-contamination
        assert "cw-clock-inst-B" not in r1.html
        assert "cw-clock-inst-A" not in r2.html

    def test_init_js_references_time_element_id(self):
        w = ClockWidget()
        cfg = ClockWidgetConfig()
        r = w.render_html(cfg, _cell(), "abc")
        assert r.init_js is not None
        assert "cw-clock-time-abc" in r.init_js
        assert "setInterval" in r.init_js

    def test_12h_format_includes_am_pm_branch(self):
        w = ClockWidget()
        cfg = ClockWidgetConfig(format="12h")
        r = w.render_html(cfg, _cell(), "abc")
        assert r.init_js is not None
        assert "'12h'" in r.init_js
        assert "PM" in r.init_js and "AM" in r.init_js

    def test_24h_format_no_am_pm_substitution(self):
        w = ClockWidget()
        cfg = ClockWidgetConfig(format="24h")
        r = w.render_html(cfg, _cell(), "abc")
        assert r.init_js is not None
        # 24h branch keeps suffix empty
        assert "'12h'" in r.init_js  # the literal branch check is still there
        assert "'24h'" in r.init_js

    def test_show_seconds_flag_present(self):
        w = ClockWidget()
        r_on = w.render_html(ClockWidgetConfig(show_seconds=True), _cell(), "a")
        r_off = w.render_html(ClockWidgetConfig(show_seconds=False), _cell(), "b")
        assert "if (true)" in r_on.init_js  # type: ignore[operator]
        assert "if (false)" in r_off.init_js  # type: ignore[operator]

    def test_show_date_adds_date_element(self):
        w = ClockWidget()
        r_off = w.render_html(ClockWidgetConfig(show_date=False), _cell(), "a")
        r_on = w.render_html(ClockWidgetConfig(show_date=True), _cell(), "b")

        assert "cw-clock-date-a" not in r_off.html
        assert "cw-clock-date-b" in r_on.html
        # The init_js date branch should be wired up only when on.
        assert r_off.init_js is not None and r_on.init_js is not None
        assert "dateEl = null" in r_off.init_js
        assert "dateEl = document.getElementById('cw-clock-date-b')" in r_on.init_js

    def test_color_propagates_to_css(self):
        w = ClockWidget()
        cfg = ClockWidgetConfig(color="#abcdef")
        r = w.render_html(cfg, _cell(), "abc")
        assert "color: #abcdef" in r.css

    def test_font_size_propagates_to_css(self):
        w = ClockWidget()
        cfg = ClockWidgetConfig(font_size_px=128)
        r = w.render_html(cfg, _cell(), "abc")
        assert "font-size: 128px" in r.css


class TestShrinkToFit:
    def test_default_off_is_byte_identical_to_no_field(self):
        w = ClockWidget()
        a = w.render_html(ClockWidgetConfig(font_size_px=64), _cell(), "x")
        b = w.render_html(ClockWidgetConfig(font_size_px=64, shrink_to_fit=False), _cell(), "x")
        assert a.html == b.html
        assert a.css == b.css
        assert a.js == b.js
        assert a.init_js == b.init_js

    def test_default_off_emits_no_autofit_code(self):
        w = ClockWidget()
        out = w.render_html(ClockWidgetConfig(font_size_px=64, shrink_to_fit=False), _cell(), "x")
        assert out.js == ""
        assert "__cwFit" not in out.html
        assert "__cwFit" not in out.css
        assert "__cwFit" not in (out.init_js or "")
        assert "cw-clock-inner-" not in out.html

    def test_on_path_emits_autofit_js_and_init(self):
        w = ClockWidget()
        out = w.render_html(ClockWidgetConfig(font_size_px=64, shrink_to_fit=True), _cell(), "abcd")
        assert "window.__cwFit" in out.js
        assert "window.__cwFitObserve" in out.js
        inner_id = "cw-clock-inner-abcd"
        assert inner_id in out.html
        assert inner_id in out.init_js
        assert "__cwFitObserve" in out.init_js
        assert "font-size: 64px" in out.css

    def test_default_config_includes_shrink_to_fit_false(self):
        assert ClockWidget().default_config()["shrink_to_fit"] is False
