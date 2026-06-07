"""Ticker widget — horizontal/vertical scrolling marquee, CSS-only.

Uses an instance-scoped ``@keyframes`` so two ticker widgets in the
same bundle scroll independently.  The animation runs entirely in CSS
(no JS, no ``init_js``) so it survives JS errors elsewhere in the
bundle and is dead-cheap on the Pi GPU.

Phase 1B scope: ``scroll`` mode only (text loops continuously in one
direction).  A ``bounce`` mode is planned as a Round-2 *variant* PR
to validate that the plugin contract supports new behaviours without
core changes.

Instance scoping is mandatory — the bundle builder rejects any
widget whose CSS isn't scoped to ``instance_id``.  Here that means
both the wrapper class (``cw-ticker-{instance_id}``) AND the
keyframe name (``ticker-scroll-{instance_id}``) include the UUID.
"""

from __future__ import annotations

import html
import re
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell


_FONT_STACKS: dict[str, str] = {
    "sans": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "serif": "Georgia, Cambria, 'Times New Roman', serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
}


class TickerWidgetConfig(BaseModel):
    """User-editable config for :class:`TickerWidget`."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=4096)
    # Pixels per second the marquee advances.  20–500 covers
    # "barely-moving billboard" through "Wall-Street-style stock
    # ticker" without giving the editor a footgun.
    speed_px_per_sec: int = Field(default=100, ge=20, le=500)
    direction: Literal["left", "right"] = "left"
    # Round-2 extensibility addition — ``scroll`` (default) is the
    # classic infinite marquee.  ``bounce`` reverses every cycle so
    # the text oscillates instead of looping.  Pure config addition;
    # no core changes required.
    mode: Literal["scroll", "bounce"] = "scroll"
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    # Background accepts either a #RRGGBB hex color or the literal
    # "transparent" so a ticker can overlay another widget / image
    # without painting an opaque strip behind it.
    background: str = Field(default="#000000")
    font_family: str = Field(default="sans")
    font_size_px: int = Field(default=48, ge=8, le=512)
    # Spacing (px) between the end of one copy and the start of the
    # next.  Lets long messages "breathe" before they repeat.
    gap_px: int = Field(default=100, ge=0, le=2000)

    @field_validator("font_family")
    @classmethod
    def _font_in_allowlist(cls, v: str) -> str:
        if v not in _FONT_STACKS:
            allowed = ", ".join(sorted(_FONT_STACKS))
            raise ValueError(f"font_family must be one of: {allowed}")
        return v

    @field_validator("background")
    @classmethod
    def _bg_color_or_transparent(cls, v: str) -> str:
        if v == "transparent":
            return v
        if not re.fullmatch(r"#[0-9a-fA-F]{6}", v):
            raise ValueError(
                "background must be a #RRGGBB hex color or 'transparent'"
            )
        return v


class TickerWidget(Widget):
    """Continuous-scroll text marquee."""

    slug: ClassVar[str] = "ticker"
    display_name: ClassVar[str] = "Scrolling Ticker"
    icon: ClassVar[str] = "📰"
    ConfigSchema: ClassVar[type[BaseModel]] = TickerWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        return {
            "text": "Breaking news goes here",
            "speed_px_per_sec": 100,
            "direction": "left",
            "mode": "scroll",
            "color": "#ffffff",
            "background": "#000000",
            "font_family": "sans",
            "font_size_px": 48,
            "gap_px": 100,
        }

    def editor_template(self) -> str:
        return "composed/widgets/ticker.html"

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        # Ticker has no asset deps; ctx is ignored.
        del ctx
        assert isinstance(config, TickerWidgetConfig), (
            "TickerWidget.render_html expects a TickerWidgetConfig instance"
        )

        wrapper_class = f"cw-ticker-{instance_id}"
        track_class = f"cw-ticker-track-{instance_id}"
        item_class = f"cw-ticker-item-{instance_id}"
        kf_name = f"ticker-scroll-{instance_id}"
        font_stack = _FONT_STACKS[config.font_family]

        escaped_text = html.escape(config.text)

        # Single copy of the text — the same model the live editor
        # preview uses.  ``padding-left: calc(100% + gap)`` pushes the
        # text fully off the right edge of the viewport; animating
        # ``translateX`` from 0 to -100% (of the element's own width,
        # which includes that padding) carries the text all the way off
        # the left.  The result is a clean wrap loop: the text scrolls
        # across, exits left, then re-enters from the right after a
        # one-viewport (+gap) gap.
        #
        # The old two-copy ``translateX(-50%)`` trick produced a
        # seamless back-to-back repeat, which for text shorter than the
        # viewport showed a visible duplicate mid-screen.  A single copy
        # never duplicates.
        html_out = (
            f'<div class="{wrapper_class}">'
            f'<div class="{track_class}">'
            f'<span class="{item_class}">{escaped_text}</span>'
            f"</div>"
            f"</div>"
        )

        # Duration in seconds for one full cycle.  We base it on the
        # 1920-px canvas width (the only canvas size in v1) so the
        # editor's "speed_px_per_sec" maps to a stable real-world
        # speed regardless of the widget's actual cell width.
        # Phase 5+ (per-device canvas sizes) will revisit this.
        duration_s = max(1.0, 1920.0 / max(1, config.speed_px_per_sec))

        # Direction: ``left`` scrolls content right-to-left (text moves
        # in negative X), ``right`` is the reverse via
        # ``animation-direction``.
        # In bounce mode the animation alternates every cycle so the
        # text oscillates between the two end points instead of
        # looping.  We combine direction + bounce via the
        # ``alternate`` / ``alternate-reverse`` keywords.
        if config.mode == "bounce":
            anim_dir = (
                "alternate" if config.direction == "left" else "alternate-reverse"
            )
        else:
            anim_dir = "normal" if config.direction == "left" else "reverse"

        css_out = (
            f".{wrapper_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: flex;\n"
            f"  align-items: center;\n"
            f"  overflow: hidden;\n"
            f"  background: {config.background};\n"
            f"  color: {config.color};\n"
            f"  font-family: {font_stack};\n"
            f"  font-size: {config.font_size_px}px;\n"
            f"  white-space: nowrap;\n"
            f"}}\n"
            f".{track_class} {{\n"
            f"  display: block;\n"
            f"  width: 100%;\n"
            f"  overflow: hidden;\n"
            f"}}\n"
            f".{item_class} {{\n"
            f"  display: inline-block;\n"
            f"  white-space: nowrap;\n"
            f"  padding-left: calc(100% + {config.gap_px}px);\n"
            f"  animation: {kf_name} {duration_s:.3f}s linear infinite {anim_dir};\n"
            f"  will-change: transform;\n"
            f"}}\n"
            f"@keyframes {kf_name} {{\n"
            f"  from {{ transform: translateX(0); }}\n"
            f"  to {{ transform: translateX(-100%); }}\n"
            f"}}"
        )

        return WidgetRender(
            html=html_out,
            css=css_out,
        )
