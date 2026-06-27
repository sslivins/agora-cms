"""Trivial text widget — Phase 1A proof-of-concept.

Renders a single piece of HTML-escaped text in a chosen color,
size, and font family.  Intentionally minimal: enough to prove the
end-to-end bundle build + publish + device delivery pipeline
without dragging in font files, image refs, or external resources.

Fonts are restricted to a small system-stack allowlist (``sans``,
``serif``, ``mono``) so Phase 1A bundles stay self-contained — no
.woff payloads to embed yet.  Phase 1B adds a richer font story
(image widget + uploadable font assets).

Instance scoping rule: every CSS class this widget emits includes
``instance_id`` so two text widgets in the same bundle can have
different styling without colliding.
"""

from __future__ import annotations

import html
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell
from cms.composed.widgets._animation import (
    ANIMATIONS,
    ANIMATION_SPEEDS,
    build_animation_css,
)
from cms.composed.widgets._autofit import (
    AUTOFIT_JS,
    autofit_inner_init_js,
)


# Allowlist of font-family slugs → emitted CSS font stack.  Keeping
# this server-side means malicious config never reaches the bundle
# as a raw font-family string.
_FONT_STACKS: dict[str, str] = {
    "sans": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "serif": "Georgia, Cambria, 'Times New Roman', serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
    # Round-2 extensibility additions — proving new font slugs can be
    # added purely via this allowlist, no core changes required.
    "display": "'Impact', 'Haettenschweiler', 'Arial Narrow Bold', sans-serif",
    "handwritten": "'Comic Sans MS', 'Bradley Hand', cursive",
}


class TextWidgetConfig(BaseModel):
    """User-editable config for :class:`TextWidget`."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=4096)
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    font_size_px: int = Field(default=48, ge=8, le=512)
    font_family: str = Field(default="sans")
    # Round-2 extensibility additions — bold / italic are pure CSS
    # output toggles, no core contract changes.
    bold: bool = False
    italic: bool = False
    # When true, font size auto-scales to fill the widget box and the
    # manual ``font_size_px`` is ignored at render time (kept only as the
    # pre-JS starting value).  Default false → byte-identical legacy render.
    shrink_to_fit: bool = False
    # Whole-text motion effect (iMessage-style).  ``"none"`` (default)
    # renders byte-identically to the legacy widget — no extra markup or
    # CSS.  Any other value must be in the server-side allowlist
    # (cms.composed.widgets._animation), so raw CSS can never reach the
    # bundle through config.
    animation: str = "none"
    animation_speed: str = "normal"

    @field_validator("font_family")
    @classmethod
    def _font_in_allowlist(cls, v: str) -> str:
        if v not in _FONT_STACKS:
            allowed = ", ".join(sorted(_FONT_STACKS))
            raise ValueError(
                f"font_family must be one of: {allowed}"
            )
        return v

    @field_validator("animation")
    @classmethod
    def _animation_in_allowlist(cls, v: str) -> str:
        if v not in ANIMATIONS:
            allowed = ", ".join(ANIMATIONS)
            raise ValueError(f"animation must be one of: {allowed}")
        return v

    @field_validator("animation_speed")
    @classmethod
    def _speed_in_allowlist(cls, v: str) -> str:
        if v not in ANIMATION_SPEEDS:
            allowed = ", ".join(ANIMATION_SPEEDS)
            raise ValueError(f"animation_speed must be one of: {allowed}")
        return v


class TextWidget(Widget):
    """Static text — the Phase 1A proof widget."""

    slug: ClassVar[str] = "text"
    display_name: ClassVar[str] = "Text"
    icon: ClassVar[str] = "🅰"
    ConfigSchema: ClassVar[type[BaseModel]] = TextWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        return {
            "text": "Text",
            "color": "#ffffff",
            "font_size_px": 48,
            "font_family": "sans",
            "bold": False,
            "italic": False,
            "shrink_to_fit": False,
            "animation": "none",
            "animation_speed": "normal",
        }

    def editor_template(self) -> str:
        # Editor UI ships in Phase 2; the path is reserved here so
        # the abstract-base contract is satisfied for Phase 1A.
        return "composed/widgets/text.html"

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        # text widget has no asset dependencies; ctx is ignored.
        del ctx
        # The base class typing uses BaseModel; narrow for attr access.
        # validate_layout always calls ConfigSchema.model_validate before
        # forwarding, so this assertion guards against direct misuse.
        assert isinstance(config, TextWidgetConfig), (
            "TextWidget.render_html expects a TextWidgetConfig instance"
        )

        escaped_text = html.escape(config.text)
        css_class = f"cw-text-{instance_id}"
        font_stack = _FONT_STACKS[config.font_family]

        font_weight = "700" if config.bold else "400"
        font_style = "italic" if config.italic else "normal"

        if config.shrink_to_fit:
            return self._render_shrink(
                escaped_text=escaped_text,
                css_class=css_class,
                instance_id=instance_id,
                font_stack=font_stack,
                font_weight=font_weight,
                font_style=font_style,
                config=config,
            )

        html_out = f'<div class="{css_class}">{escaped_text}</div>'

        css_out = (
            f".{css_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: flex;\n"
            f"  align-items: center;\n"
            f"  justify-content: center;\n"
            f"  text-align: center;\n"
            f"  color: {config.color};\n"
            f"  font-size: {config.font_size_px}px;\n"
            f"  font-family: {font_stack};\n"
            f"  font-weight: {font_weight};\n"
            f"  font-style: {font_style};\n"
            f"  overflow: hidden;\n"
            f"  word-wrap: break-word;\n"
            f"}}"
        )

        if config.animation != "none":
            anim_class = f"cw-text-anim-{instance_id}"
            anim = build_animation_css(
                config.animation,
                instance_id=instance_id,
                anim_selector=f".{anim_class}",
                speed=config.animation_speed,
            )
            if anim is not None:
                html_out = (
                    f'<div class="{css_class}">'
                    f'<span class="{anim_class}">{escaped_text}</span>'
                    f"</div>"
                )
                css_out += "\n" + self._animation_css(
                    css_class=css_class,
                    anim_class=anim_class,
                    anim=anim,
                )

        return WidgetRender(
            html=html_out,
            css=css_out,
        )

    @staticmethod
    def _animation_css(*, css_class: str, anim_class: str, anim) -> str:
        """Compose the box + animated-element CSS for an active effect.

        Adds ``perspective`` to the bounded box for 3D effects, makes the
        animated span an ``inline-block`` so transforms have a box to act
        on, then appends the scoped ``@keyframes`` + animation rule.
        """
        box = ""
        if anim.needs_3d:
            box = (
                f".{css_class} {{ perspective: 600px; }}\n"
                f".{anim_class} {{ transform-style: preserve-3d; }}\n"
            )
        return (
            f"{box}"
            f".{anim_class} {{ display: inline-block; }}\n"
            f"{anim.css}"
        )

    def _render_shrink(
        self,
        *,
        escaped_text: str,
        css_class: str,
        instance_id: str,
        font_stack: str,
        font_weight: str,
        font_style: str,
        config: TextWidgetConfig,
    ) -> WidgetRender:
        """Shrink-to-fit variant: text auto-scales to fill the box.

        Outer ``.{css_class}`` is the bounded, flex-centered box; an inner
        ``#cw-text-inner-{instance_id}`` holds the text and is the element
        whose font size the autofit JS binary-searches.
        """
        inner_id = f"cw-text-inner-{instance_id}"
        inner_class = f"{css_class}-inner"

        html_out = (
            f'<div class="{css_class}">'
            f'<div id="{inner_id}" class="{inner_class}">{escaped_text}</div>'
            f"</div>"
        )

        css_out = (
            f".{css_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: flex;\n"
            f"  align-items: center;\n"
            f"  justify-content: center;\n"
            f"  text-align: center;\n"
            f"  color: {config.color};\n"
            f"  font-family: {font_stack};\n"
            f"  font-weight: {font_weight};\n"
            f"  font-style: {font_style};\n"
            f"  overflow: hidden;\n"
            f"}}\n"
            f".{inner_class} {{\n"
            # Starting size before JS runs; immediately overridden by fit.
            f"  font-size: {config.font_size_px}px;\n"
            f"  max-width: 100%;\n"
            f"  line-height: 1.1;\n"
            f"  word-wrap: break-word;\n"
            f"}}"
        )

        init_js = autofit_inner_init_js(inner_id)

        if config.animation != "none":
            anim = build_animation_css(
                config.animation,
                instance_id=instance_id,
                anim_selector=f"#{inner_id}",
                speed=config.animation_speed,
            )
            if anim is not None:
                if anim.needs_3d:
                    css_out += (
                        f"\n.{css_class} {{ perspective: 600px; }}\n"
                        f"#{inner_id} {{ transform-style: preserve-3d; }}"
                    )
                css_out += "\n" + anim.css

        return WidgetRender(
            html=html_out,
            css=css_out,
            js=AUTOFIT_JS,
            init_js=init_js,
        )
