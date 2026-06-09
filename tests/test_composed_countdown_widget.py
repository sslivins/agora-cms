"""Tests for cms.composed.widgets.countdown.CountdownWidget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import cms.composed.widgets  # noqa: F401
from cms.composed.registry import get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.countdown import CountdownWidget, CountdownWidgetConfig


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


class TestCountdownWidgetConfig:
    def test_defaults(self):
        c = CountdownWidgetConfig()
        assert c.target == "2030-01-01T00:00:00"
        assert c.direction == "down"
        assert c.label == ""
        assert c.completed_text == ""
        assert c.show_days is True
        assert c.show_hours is True
        assert c.show_minutes is True
        assert c.show_seconds is False
        assert c.color == "#ffffff"
        assert c.font_family == "sans"
        assert c.font_size_px == 96

    def test_direction_allowlist(self):
        CountdownWidgetConfig(direction="down")
        CountdownWidgetConfig(direction="up")
        with pytest.raises(ValidationError):
            CountdownWidgetConfig(direction="sideways")  # type: ignore[arg-type]

    def test_target_accepts_minute_and_second_precision(self):
        CountdownWidgetConfig(target="2031-12-31T23:59")
        CountdownWidgetConfig(target="2031-12-31T23:59:30")

    def test_target_rejects_garbage(self):
        with pytest.raises(ValidationError):
            CountdownWidgetConfig(target="next tuesday")

    def test_target_rejects_timezone_aware(self):
        with pytest.raises(ValidationError):
            CountdownWidgetConfig(target="2030-01-01T00:00:00+00:00")

    def test_font_allowlist(self):
        for ok in ("sans", "serif", "mono"):
            CountdownWidgetConfig(font_family=ok)
        with pytest.raises(ValidationError):
            CountdownWidgetConfig(font_family="comic-sans")

    def test_color_must_be_hex_6(self):
        with pytest.raises(ValidationError):
            CountdownWidgetConfig(color="red")

    def test_font_size_bounds(self):
        CountdownWidgetConfig(font_size_px=8)
        CountdownWidgetConfig(font_size_px=512)
        with pytest.raises(ValidationError):
            CountdownWidgetConfig(font_size_px=7)
        with pytest.raises(ValidationError):
            CountdownWidgetConfig(font_size_px=513)

    def test_label_and_completed_text_length_capped(self):
        with pytest.raises(ValidationError):
            CountdownWidgetConfig(label="x" * 121)
        with pytest.raises(ValidationError):
            CountdownWidgetConfig(completed_text="x" * 121)

    def test_at_least_one_unit_required(self):
        with pytest.raises(ValidationError):
            CountdownWidgetConfig(
                show_days=False,
                show_hours=False,
                show_minutes=False,
                show_seconds=False,
            )

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            CountdownWidgetConfig(timezone="UTC")  # type: ignore[call-arg]


class TestCountdownWidgetRegistry:
    def test_registered(self):
        w = get_registry().get("countdown")
        assert isinstance(w, CountdownWidget)
        assert w.slug == "countdown"


class TestCountdownWidgetRender:
    def test_no_declared_assets(self):
        w = CountdownWidget()
        cfg = CountdownWidgetConfig()
        assert w.declared_asset_ids(cfg) == []

    def test_html_and_css_are_instance_scoped(self):
        w = CountdownWidget()
        cfg = CountdownWidgetConfig()

        r1 = w.render_html(cfg, _cell(), "inst-A")
        r2 = w.render_html(cfg, _cell(), "inst-B")

        assert "cw-countdown-inst-A" in r1.html
        assert "cw-countdown-inst-A" in r1.css
        assert "cw-countdown-time-inst-A" in r1.html
        assert "cw-countdown-inst-B" in r2.html
        # No cross-contamination
        assert "cw-countdown-inst-B" not in r1.html
        assert "cw-countdown-inst-A" not in r2.html

    def test_init_js_references_time_element_id_and_ticks(self):
        w = CountdownWidget()
        cfg = CountdownWidgetConfig()
        r = w.render_html(cfg, _cell(), "abc")
        assert r.init_js is not None
        assert "cw-countdown-time-abc" in r.init_js
        assert "setInterval" in r.init_js

    def test_target_baked_as_device_local_date_constructor(self):
        w = CountdownWidget()
        cfg = CountdownWidgetConfig(target="2030-06-15T09:30:45")
        r = w.render_html(cfg, _cell(), "abc")
        assert r.init_js is not None
        # month is zero-indexed in JS Date constructor (June -> 5)
        assert "new Date(2030, 5, 15, 9, 30, 45).getTime()" in r.init_js

    def test_direction_propagates_to_init_js(self):
        w = CountdownWidget()
        r_down = w.render_html(CountdownWidgetConfig(direction="down"), _cell(), "a")
        r_up = w.render_html(CountdownWidgetConfig(direction="up"), _cell(), "b")
        assert "var dir = 'down'" in r_down.init_js  # type: ignore[operator]
        assert "var dir = 'up'" in r_up.init_js  # type: ignore[operator]

    def test_unit_flags_propagate_to_init_js(self):
        w = CountdownWidget()
        cfg = CountdownWidgetConfig(
            show_days=True,
            show_hours=False,
            show_minutes=True,
            show_seconds=False,
        )
        r = w.render_html(cfg, _cell(), "abc")
        assert r.init_js is not None
        assert "[true, 86400, 'd']" in r.init_js
        assert "[false, 3600, 'h']" in r.init_js
        assert "[true, 60, 'm']" in r.init_js
        assert "[false, 1, 's']" in r.init_js

    def test_completed_text_injected_as_json_string_literal(self):
        w = CountdownWidget()
        # A quote + closing-script-ish payload must be escaped, not break JS.
        cfg = CountdownWidgetConfig(completed_text='Done! "go"')
        r = w.render_html(cfg, _cell(), "abc")
        assert r.init_js is not None
        assert 'var completed = "Done! \\"go\\""' in r.init_js

    def test_label_rendered_and_html_escaped(self):
        w = CountdownWidget()
        r_off = w.render_html(CountdownWidgetConfig(label=""), _cell(), "a")
        r_on = w.render_html(
            CountdownWidgetConfig(label="Sale <ends>"), _cell(), "b"
        )
        assert "cw-countdown-a-label" not in r_off.html
        assert "cw-countdown-b-label" in r_on.html
        assert "Sale &lt;ends&gt;" in r_on.html
        assert "<ends>" not in r_on.html

    def test_color_propagates_to_css(self):
        w = CountdownWidget()
        cfg = CountdownWidgetConfig(color="#abcdef")
        r = w.render_html(cfg, _cell(), "abc")
        assert "color: #abcdef" in r.css

    def test_font_size_propagates_to_css(self):
        w = CountdownWidget()
        cfg = CountdownWidgetConfig(font_size_px=128)
        r = w.render_html(cfg, _cell(), "abc")
        assert "font-size: 128px" in r.css
