"""Tests for cms.composed.widgets.ticker.TickerWidget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import cms.composed.widgets  # noqa: F401
from cms.composed.registry import get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.ticker import TickerWidget, TickerWidgetConfig


def _cell() -> Cell:
    return Cell(row=8, col=1, rowspan=1, colspan=12)


class TestTickerWidgetConfig:
    def test_minimum_valid_config(self):
        c = TickerWidgetConfig(text="hi")
        assert c.text == "hi"
        assert c.direction == "left"
        assert c.speed_px_per_sec == 100

    def test_text_required(self):
        with pytest.raises(ValidationError):
            TickerWidgetConfig()  # type: ignore[call-arg]

    def test_text_min_length(self):
        with pytest.raises(ValidationError):
            TickerWidgetConfig(text="")

    def test_text_max_length(self):
        with pytest.raises(ValidationError):
            TickerWidgetConfig(text="x" * 4097)

    def test_speed_bounds(self):
        TickerWidgetConfig(text="hi", speed_px_per_sec=20)
        TickerWidgetConfig(text="hi", speed_px_per_sec=500)
        with pytest.raises(ValidationError):
            TickerWidgetConfig(text="hi", speed_px_per_sec=19)
        with pytest.raises(ValidationError):
            TickerWidgetConfig(text="hi", speed_px_per_sec=501)

    def test_direction_allowlist(self):
        TickerWidgetConfig(text="hi", direction="left")
        TickerWidgetConfig(text="hi", direction="right")
        with pytest.raises(ValidationError):
            TickerWidgetConfig(text="hi", direction="up")  # type: ignore[arg-type]

    def test_font_allowlist(self):
        for ok in ("sans", "serif", "mono"):
            TickerWidgetConfig(text="hi", font_family=ok)
        with pytest.raises(ValidationError):
            TickerWidgetConfig(text="hi", font_family="cursive")

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            TickerWidgetConfig(text="hi", scrolling=True)  # type: ignore[call-arg]


class TestTickerWidgetRegistry:
    def test_registered(self):
        w = get_registry().get("ticker")
        assert isinstance(w, TickerWidget)
        assert w.slug == "ticker"


class TestTickerWidgetRender:
    def test_no_declared_assets(self):
        w = TickerWidget()
        cfg = TickerWidgetConfig(text="hi")
        assert w.declared_asset_ids(cfg) == []

    def test_html_and_css_are_instance_scoped(self):
        w = TickerWidget()
        cfg = TickerWidgetConfig(text="news")

        r1 = w.render_html(cfg, _cell(), "inst-A")
        r2 = w.render_html(cfg, _cell(), "inst-B")

        for cls in (
            "cw-ticker-inst-A",
            "cw-ticker-track-inst-A",
            "cw-ticker-item-inst-A",
        ):
            assert cls in r1.html, f"{cls} should be in html"
            assert cls in r1.css, f"{cls} should be in css"

        # @keyframes name is also instance-scoped so two tickers can
        # coexist with different animation durations.
        assert "ticker-scroll-inst-A" in r1.css
        assert "ticker-scroll-inst-B" in r2.css
        assert "ticker-scroll-inst-B" not in r1.css
        assert "ticker-scroll-inst-A" not in r2.css

    def test_text_is_html_escaped_and_duplicated(self):
        w = TickerWidget()
        cfg = TickerWidgetConfig(text="<b>hi</b> & bye")
        r = w.render_html(cfg, _cell(), "abc")

        assert "<b>hi</b>" not in r.html
        # The duplicate-content marquee trick: text appears twice
        assert r.html.count("&lt;b&gt;hi&lt;/b&gt;") == 2
        assert "&amp;" in r.html
        # Second copy is aria-hidden to avoid duplicate-announcement
        assert 'aria-hidden="true"' in r.html

    def test_direction_left_is_normal_anim(self):
        w = TickerWidget()
        r = w.render_html(
            TickerWidgetConfig(text="x", direction="left"),
            _cell(),
            "a",
        )
        assert "animation:" in r.css
        assert " normal" in r.css.replace("\n", " ")

    def test_direction_right_is_reverse_anim(self):
        w = TickerWidget()
        r = w.render_html(
            TickerWidgetConfig(text="x", direction="right"),
            _cell(),
            "a",
        )
        assert "reverse" in r.css

    def test_speed_affects_duration(self):
        # 1920-px canvas; 100 px/s → 19.2 s, 200 px/s → 9.6 s.
        w = TickerWidget()
        r_slow = w.render_html(
            TickerWidgetConfig(text="x", speed_px_per_sec=100),
            _cell(),
            "a",
        )
        r_fast = w.render_html(
            TickerWidgetConfig(text="x", speed_px_per_sec=200),
            _cell(),
            "b",
        )
        assert "19.200s" in r_slow.css
        assert "9.600s" in r_fast.css

    def test_gap_propagates_to_css(self):
        w = TickerWidget()
        r = w.render_html(
            TickerWidgetConfig(text="x", gap_px=250),
            _cell(),
            "a",
        )
        assert "gap: 250px" in r.css
        assert "padding-right: 250px" in r.css

    def test_color_and_background_propagate(self):
        w = TickerWidget()
        r = w.render_html(
            TickerWidgetConfig(
                text="x", color="#abcdef", background="#123456",
            ),
            _cell(),
            "a",
        )
        assert "color: #abcdef" in r.css
        assert "background: #123456" in r.css

    def test_no_init_js_pure_css_marquee(self):
        # The CSS-only marquee survives JS errors elsewhere in the
        # bundle.  If we ever switch to JS-driven scrolling we want to
        # know about it loudly.
        w = TickerWidget()
        r = w.render_html(TickerWidgetConfig(text="x"), _cell(), "a")
        assert r.init_js is None
        assert r.js == ""
