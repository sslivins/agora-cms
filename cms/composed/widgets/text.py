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


# Allowlist of font-family slugs → emitted CSS font stack.  Keeping
# this server-side means malicious config never reaches the bundle
# as a raw font-family string.
_FONT_STACKS: dict[str, str] = {
    "sans": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "serif": "Georgia, Cambria, 'Times New Roman', serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
}


class TextWidgetConfig(BaseModel):
    """User-editable config for :class:`TextWidget`."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1, max_length=4096)
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    font_size_px: int = Field(default=48, ge=8, le=512)
    font_family: str = Field(default="sans")

    @field_validator("font_family")
    @classmethod
    def _font_in_allowlist(cls, v: str) -> str:
        if v not in _FONT_STACKS:
            allowed = ", ".join(sorted(_FONT_STACKS))
            raise ValueError(
                f"font_family must be one of: {allowed}"
            )
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
            f"  overflow: hidden;\n"
            f"  word-wrap: break-word;\n"
            f"}}"
        )

        return WidgetRender(
            html=html_out,
            css=css_out,
        )
