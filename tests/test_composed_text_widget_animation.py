"""Tests for animated-text effects on cms.composed.widgets.text.TextWidget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import cms.composed.widgets  # noqa: F401
from cms.composed.schema import Cell
from cms.composed.widgets._animation import ANIMATIONS, build_animation_css
from cms.composed.widgets.text import TextWidget, TextWidgetConfig

IID = "11111111-1111-1111-1111-111111111111"
IID2 = "22222222-2222-2222-2222-222222222222"


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=1, colspan=1)


def _render(**cfg):
    config = TextWidgetConfig(**cfg)
    return TextWidget().render_html(config, _cell(), IID)


class TestAnimationConfig:
    def test_defaults_none(self):
        c = TextWidgetConfig(text="hi")
        assert c.animation == "none"
        assert c.animation_speed == "normal"

    def test_all_allowlisted_effects_accepted(self):
        for slug in ANIMATIONS:
            TextWidgetConfig(text="hi", animation=slug)

    def test_invalid_animation_rejected(self):
        for bad in ("spin", "BIG", "", "javascript", "fx-big"):
            with pytest.raises(ValidationError):
                TextWidgetConfig(text="hi", animation=bad)

    def test_speed_allowlist(self):
        for ok in ("slow", "normal", "fast"):
            TextWidgetConfig(text="hi", animation_speed=ok)
        for bad in ("turbo", "", "Normal", "1"):
            with pytest.raises(ValidationError):
                TextWidgetConfig(text="hi", animation_speed=bad)


class TestAnimationRender:
    def test_none_is_legacy_render(self):
        r = _render(text="Hi", animation="none")
        assert "@keyframes" not in r.css
        assert "cw-text-anim" not in r.html
        assert r.html == f'<div class="cw-text-{IID}">Hi</div>'

    def test_effect_emits_scoped_keyframes_and_span(self):
        r = _render(text="Hi", animation="big")
        assert f'<span class="cw-text-anim-{IID}">Hi</span>' in r.html
        assert f"@keyframes cw-kf-{IID}" in r.css
        assert f".cw-text-anim-{IID} {{" in r.css
        assert "animation:" in r.css

    def test_3d_effect_adds_perspective(self):
        for slug in ("nod", "flip"):
            r = _render(text="Hi", animation=slug)
            assert "perspective: 600px" in r.css
            assert "preserve-3d" in r.css

    def test_non_3d_effect_no_perspective(self):
        r = _render(text="Hi", animation="big")
        assert "perspective" not in r.css

    def test_shimmer_uses_background_clip(self):
        r = _render(text="Hi", animation="shimmer")
        assert "background-clip:text" in r.css

    def test_text_is_escaped_inside_anim_span(self):
        r = _render(text="<b>&", animation="big")
        assert "<b>" not in r.html
        assert "&lt;b&gt;&amp;" in r.html

    def test_speed_changes_duration(self):
        fast = _render(text="Hi", animation="big", animation_speed="fast").css
        slow = _render(text="Hi", animation="big", animation_speed="slow").css
        assert fast != slow
        # fast = 2.8 * 0.6 = 1.68s; slow = 2.8 * 1.6 = 4.48s
        assert "1.68s" in fast
        assert "4.48s" in slow

    def test_shrink_to_fit_animates_inner(self):
        r = _render(text="Hi", animation="pulse", shrink_to_fit=True)
        assert f"#cw-text-inner-{IID} {{" in r.css
        assert f"@keyframes cw-kf-{IID}" in r.css
        # autofit path still wired
        assert r.init_js is not None

    def test_instances_get_distinct_keyframes(self):
        cfg = TextWidgetConfig(text="Hi", animation="big")
        c1 = TextWidget().render_html(cfg, _cell(), IID).css
        c2 = TextWidget().render_html(cfg, _cell(), IID2).css
        assert f"cw-kf-{IID}" in c1
        assert f"cw-kf-{IID2}" in c2
        assert IID2 not in c1


class TestAnimationHelper:
    def test_none_returns_none(self):
        assert build_animation_css("none", instance_id=IID, anim_selector=".x") is None
        assert build_animation_css("bogus", instance_id=IID, anim_selector=".x") is None

    def test_scoped_name(self):
        a = build_animation_css("flip", instance_id=IID, anim_selector=".x", speed="normal")
        assert a is not None
        assert f"cw-kf-{IID}" in a.css
        assert a.needs_3d is True
