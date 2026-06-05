"""Tests for cms.composed.widgets.analog_clock.AnalogClockWidget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import cms.composed.widgets  # noqa: F401
from cms.composed.registry import get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.analog_clock import (
    AnalogClockWidget,
    AnalogClockWidgetConfig,
)


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


class TestAnalogClockWidgetConfig:
    def test_defaults(self):
        c = AnalogClockWidgetConfig()
        assert c.face_color == "#111827"
        assert c.hand_color == "#ffffff"
        assert c.second_color == "#ef4444"
        assert c.show_seconds is True
        assert c.show_ticks is True
        assert c.show_numerals is False

    def test_default_config_matches_schema(self):
        # The editor's DEFAULTS map must round-trip through the schema
        # with extra="forbid" — otherwise a freshly-placed widget fails
        # save/publish validation.
        w = AnalogClockWidget()
        AnalogClockWidgetConfig(**w.default_config())

    def test_color_must_be_hex_6(self):
        for field in ("face_color", "hand_color", "second_color"):
            with pytest.raises(ValidationError):
                AnalogClockWidgetConfig(**{field: "red"})

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            AnalogClockWidgetConfig(timezone="UTC")  # type: ignore[call-arg]


class TestAnalogClockWidgetRegistry:
    def test_registered(self):
        w = get_registry().get("analog_clock")
        assert isinstance(w, AnalogClockWidget)
        assert w.slug == "analog_clock"
        assert w.display_name == "Analog Clock"


class TestAnalogClockWidgetRender:
    def test_no_declared_assets(self):
        w = AnalogClockWidget()
        assert w.declared_asset_ids(AnalogClockWidgetConfig()) == []

    def test_html_and_css_are_instance_scoped(self):
        w = AnalogClockWidget()
        cfg = AnalogClockWidgetConfig()

        r1 = w.render_html(cfg, _cell(), "inst-A")
        r2 = w.render_html(cfg, _cell(), "inst-B")

        assert "cw-aclock-inst-A" in r1.html
        assert "cw-aclock-inst-A" in r1.css
        assert "cw-aclock-inst-B" in r2.html
        # No cross-contamination.
        assert "inst-B" not in r1.html
        assert "inst-A" not in r2.html

    def test_svg_viewbox_present(self):
        w = AnalogClockWidget()
        r = w.render_html(AnalogClockWidgetConfig(), _cell(), "abc")
        assert 'viewBox="0 0 100 100"' in r.html
        assert "<svg" in r.html

    def test_three_hand_groups_when_seconds_on(self):
        w = AnalogClockWidget()
        r = w.render_html(AnalogClockWidgetConfig(show_seconds=True), _cell(), "abc")
        assert 'id="cw-aclock-hour-abc"' in r.html
        assert 'id="cw-aclock-min-abc"' in r.html
        assert 'id="cw-aclock-sec-abc"' in r.html

    def test_second_hand_omitted_when_off(self):
        w = AnalogClockWidget()
        r = w.render_html(AnalogClockWidgetConfig(show_seconds=False), _cell(), "abc")
        assert 'id="cw-aclock-hour-abc"' in r.html
        assert 'id="cw-aclock-min-abc"' in r.html
        assert 'id="cw-aclock-sec-abc"' not in r.html
        # init_js must not look up the missing second hand.
        assert r.init_js is not None
        assert "cw-aclock-sec-abc" not in r.init_js
        assert "secEl = null" in r.init_js

    def test_init_js_rotates_and_ticks(self):
        w = AnalogClockWidget()
        r = w.render_html(AnalogClockWidgetConfig(), _cell(), "abc")
        assert r.init_js is not None
        assert "cw-aclock-hour-abc" in r.init_js
        assert "setInterval" in r.init_js
        assert "rotate(" in r.init_js

    def test_ticks_toggle(self):
        w = AnalogClockWidget()
        on = w.render_html(AnalogClockWidgetConfig(show_ticks=True), _cell(), "a")
        off = w.render_html(AnalogClockWidgetConfig(show_ticks=False), _cell(), "b")
        assert "cw-aclock-a-tick-major" in on.html
        assert "tick-major" not in off.html

    def test_numerals_toggle(self):
        w = AnalogClockWidget()
        on = w.render_html(AnalogClockWidgetConfig(show_numerals=True), _cell(), "a")
        off = w.render_html(AnalogClockWidgetConfig(show_numerals=False), _cell(), "b")
        assert "cw-aclock-a-num" in on.html
        assert "-num" not in off.html

    def test_colors_propagate_to_css(self):
        w = AnalogClockWidget()
        cfg = AnalogClockWidgetConfig(
            face_color="#010203", hand_color="#040506", second_color="#070809"
        )
        r = w.render_html(cfg, _cell(), "abc")
        assert "fill: #010203" in r.css
        assert "stroke: #040506" in r.css
        assert "stroke: #070809" in r.css

    def test_deterministic_render(self):
        w = AnalogClockWidget()
        cfg = AnalogClockWidgetConfig()
        a = w.render_html(cfg, _cell(), "abc")
        b = w.render_html(cfg, _cell(), "abc")
        assert a.html == b.html
        assert a.css == b.css
        assert a.init_js == b.init_js
