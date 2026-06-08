"""QR code widget — renders a scannable QR code for a target URL.

The QR matrix is generated **server-side at bundle-build time** with
:mod:`segno` (pure-Python, zero runtime deps) and embedded as an inline
``<svg>``.  This keeps the widget deterministic (a given config + URL
always produces byte-identical SVG), fully offline (no runtime fetch,
no external ``src=``/``href=``), and resolution-independent (the SVG
``viewBox`` scales crisply to any cell size).

No JS, no asset dependencies — the simplest widget shape in the set.

Instance scoping: the wrapper ``<div>`` class is suffixed with the
widget instance UUID so two QR widgets in the same bundle don't
collide.  The inner ``segno``/``qrline`` classes segno emits on the
SVG are never styled by us, so they can't cross-contaminate.
"""

from __future__ import annotations

from typing import ClassVar, Literal
from urllib.parse import urlparse

import segno
from pydantic import BaseModel, ConfigDict, Field, field_validator

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell

# URLs longer than this are rejected — well within QR byte capacity even
# at the highest error-correction level (version 40 / H holds ~1273
# bytes) and a sane upper bound for a sign someone is meant to scan.
_MAX_URL_LEN = 1000

# Only real, scannable web targets.  No javascript:/data:/tel: etc. —
# matches the layout-level URL allowlist (https/http only).
_ALLOWED_SCHEMES = ("http", "https")


class QrWidgetConfig(BaseModel):
    """User-editable config for :class:`QrWidget`."""

    model_config = ConfigDict(extra="forbid")

    url: str = Field(min_length=1, max_length=_MAX_URL_LEN)
    error_correction: Literal["L", "M", "Q", "H"] = "M"
    foreground: str = Field(default="#000000", pattern=r"^#[0-9a-fA-F]{6}$")
    background: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    quiet_zone: bool = True

    @field_validator("url")
    @classmethod
    def _url_scheme_allowed(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("url must not be blank")
        scheme = urlparse(v).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            allowed = ", ".join(_ALLOWED_SCHEMES)
            raise ValueError(f"url scheme must be one of: {allowed}")
        return v


class QrWidget(Widget):
    """Server-rendered QR code pointing at a URL."""

    slug: ClassVar[str] = "qr"
    display_name: ClassVar[str] = "QR Code"
    icon: ClassVar[str] = "▦"
    ConfigSchema: ClassVar[type[BaseModel]] = QrWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        return {
            "url": "https://example.com",
            "error_correction": "M",
            "foreground": "#000000",
            "background": "#ffffff",
            "quiet_zone": True,
        }

    def editor_template(self) -> str:
        return "composed/widgets/qr.html"

    def validate_semantic(self, config: BaseModel) -> list[str]:
        assert isinstance(config, QrWidgetConfig)
        errors: list[str] = []
        scheme = urlparse(config.url).scheme.lower()
        if scheme not in _ALLOWED_SCHEMES:
            errors.append(
                f"QR url scheme '{scheme}' is not allowed "
                f"(must be one of: {', '.join(_ALLOWED_SCHEMES)})"
            )
        return errors

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        # QR has no asset deps; ctx is ignored.
        del ctx, cell
        assert isinstance(config, QrWidgetConfig), (
            "QrWidget.render_html expects a QrWidgetConfig instance"
        )

        css_class = f"cw-qr-{instance_id}"

        qr = segno.make(config.url, error=config.error_correction.lower())
        # ``omitsize=True`` drops the fixed width/height and emits a
        # ``viewBox`` instead, so the matrix scales to fill its box while
        # the default ``preserveAspectRatio`` keeps it square (no
        # distortion in non-square cells — it letterboxes).
        svg = qr.svg_inline(
            omitsize=True,
            border=4 if config.quiet_zone else 0,
            dark=config.foreground,
            light=config.background,
        )

        html_out = f'<div class="{css_class}">{svg}</div>'

        # Wrapper background matches the QR light colour so the letterbox
        # gutter in a non-square cell blends seamlessly with the code.
        css_out = (
            f".{css_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: flex;\n"
            f"  align-items: center;\n"
            f"  justify-content: center;\n"
            f"  background: {config.background};\n"
            f"}}\n"
            f".{css_class} svg {{\n"
            f"  display: block;\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"}}"
        )

        return WidgetRender(html=html_out, css=css_out)
