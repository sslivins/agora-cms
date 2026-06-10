"""Date banner widget — shows the current date/day using device-local time.

Pure-JS, no asset dependencies.  Re-renders every 60 s via
``setInterval`` so the banner rolls over at local midnight without a
page reload.  Uses the browser's ``Date`` object + ``toLocaleDateString``
so the displayed date follows whatever the OS clock + locale + TZ are
set to on the device (same rationale as :mod:`cms.composed.widgets.clock`).

Instance scoping: every DOM ID + CSS class is suffixed with the widget
instance UUID so two date-banner widgets in the same bundle don't
collide.
"""

from __future__ import annotations

import html
import json
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell
from cms.composed.widgets._autofit import (
    AUTOFIT_JS,
    AUTOFIT_MAX_PX,
    AUTOFIT_MIN_PX,
)

# Font-family allowlist — kept local to this widget so it can evolve
# typography independently of clock/text.
_FONT_STACKS: dict[str, str] = {
    "sans": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "serif": "Georgia, Cambria, 'Times New Roman', serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
}

# Each format maps to a ``toLocaleDateString`` options object literal,
# baked into the init JS.  Keeping the mapping server-side means the
# device never evaluates user-controlled format code — only one of
# these fixed, CMS-authored option objects.
_FORMAT_OPTIONS: dict[str, str] = {
    # Monday, January 1, 2030
    "full": "{weekday:'long', year:'numeric', month:'long', day:'numeric'}",
    # January 1, 2030
    "long": "{year:'numeric', month:'long', day:'numeric'}",
    # Monday
    "weekday": "{weekday:'long'}",
    # Mon, Jan 1
    "short": "{weekday:'short', month:'short', day:'numeric'}",
    # 01/01/2030 (locale numeric)
    "numeric": "{year:'numeric', month:'2-digit', day:'2-digit'}",
}


class DateBannerWidgetConfig(BaseModel):
    """User-editable config for :class:`DateBannerWidget`."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["full", "long", "weekday", "short", "numeric"] = "full"
    prefix: str = Field(default="", max_length=120)
    uppercase: bool = False
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    font_family: str = Field(default="sans")
    font_size_px: int = Field(default=96, ge=8, le=512)
    # When true, font size auto-scales to fill the widget box; the manual
    # ``font_size_px`` becomes the pre-JS starting value only.  Default
    # false → byte-identical legacy render.
    shrink_to_fit: bool = False

    @field_validator("font_family")
    @classmethod
    def _font_in_allowlist(cls, v: str) -> str:
        if v not in _FONT_STACKS:
            allowed = ", ".join(sorted(_FONT_STACKS))
            raise ValueError(f"font_family must be one of: {allowed}")
        return v


# Per-instance init script.  ``$NAME`` placeholders (not ``{name}``) so
# literal ``{`` in the JS body don't need brace-doubling.
_DATEBANNER_INIT_JS_TEMPLATE = """
var dateEl = document.getElementById($DATE_ID_LIT);
var prefix = $PREFIX_LIT;
var upper = $UPPERCASE;
function render() {
  if (!dateEl) return;
  var d = new Date();
  var s = d.toLocaleDateString(undefined, $OPTIONS);
  if (prefix) s = prefix + ' ' + s;
  if (upper) s = s.toUpperCase();
  dateEl.textContent = s;
}
render();
setInterval(render, 60000);
""".strip()


def _build_datebanner_init_js(
    *,
    date_id: str,
    options: str,
    prefix: str,
    uppercase: bool,
) -> str:
    return (
        _DATEBANNER_INIT_JS_TEMPLATE
        .replace("$DATE_ID_LIT", f"'{date_id}'")
        .replace("$OPTIONS", options)
        .replace("$PREFIX_LIT", json.dumps(prefix))
        .replace("$UPPERCASE", "true" if uppercase else "false")
    )


# Shrink-to-fit init: identical date logic, but refits the font after each
# render (initial + every 60 s rollover) and on box resize.  Kept as a
# SEPARATE template so the default (non-autofit) path stays byte-identical.
_DATEBANNER_INIT_JS_AUTOFIT_TEMPLATE = """
var dateEl = document.getElementById($DATE_ID_LIT);
var prefix = $PREFIX_LIT;
var upper = $UPPERCASE;
function refit() {
  if (dateEl && window.__cwFit) window.__cwFit(dateEl, $MAX_PX, $MIN_PX);
}
function render() {
  if (!dateEl) return;
  var d = new Date();
  var s = d.toLocaleDateString(undefined, $OPTIONS);
  if (prefix) s = prefix + ' ' + s;
  if (upper) s = s.toUpperCase();
  dateEl.textContent = s;
  refit();
}
render();
if (dateEl && dateEl.parentElement && typeof ResizeObserver !== 'undefined') {
  try { new ResizeObserver(refit).observe(dateEl.parentElement); }
  catch (e) { window.addEventListener('resize', refit); }
} else {
  window.addEventListener('resize', refit);
}
setInterval(render, 60000);
""".strip()


def _build_datebanner_init_js_autofit(
    *,
    date_id: str,
    options: str,
    prefix: str,
    uppercase: bool,
) -> str:
    return (
        _DATEBANNER_INIT_JS_AUTOFIT_TEMPLATE
        .replace("$DATE_ID_LIT", f"'{date_id}'")
        .replace("$OPTIONS", options)
        .replace("$PREFIX_LIT", json.dumps(prefix))
        .replace("$UPPERCASE", "true" if uppercase else "false")
        .replace("$MAX_PX", str(AUTOFIT_MAX_PX))
        .replace("$MIN_PX", str(AUTOFIT_MIN_PX))
    )


class DateBannerWidget(Widget):
    """Current date / day-of-week banner."""

    slug: ClassVar[str] = "datebanner"
    display_name: ClassVar[str] = "Date Banner"
    icon: ClassVar[str] = "📅"
    ConfigSchema: ClassVar[type[BaseModel]] = DateBannerWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        return {
            "format": "full",
            "prefix": "",
            "uppercase": False,
            "color": "#ffffff",
            "font_family": "sans",
            "font_size_px": 96,
            "shrink_to_fit": False,
        }

    def editor_template(self) -> str:
        return "composed/widgets/datebanner.html"

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        # No asset deps; ctx + cell unused.
        del ctx, cell
        assert isinstance(config, DateBannerWidgetConfig), (
            "DateBannerWidget.render_html expects a DateBannerWidgetConfig"
        )

        css_class = f"cw-datebanner-{instance_id}"
        date_id = f"cw-datebanner-date-{instance_id}"
        font_stack = _FONT_STACKS[config.font_family]
        options = _FORMAT_OPTIONS[config.format]

        # Static fallback text in the markup (escaped) so the banner is
        # not blank for the split second before init_js runs, and so a
        # JS-less snapshot still shows something sensible.  The runtime
        # immediately overwrites it with the live, locale-formatted date.
        placeholder = html.escape(
            f"{config.prefix} \u2026" if config.prefix else "\u2026"
        )
        html_out = (
            f'<div class="{css_class}">'
            f'<div id="{date_id}" class="{css_class}-date">{placeholder}</div>'
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
            f"}}\n"
            f".{css_class}-date {{\n"
            f"  font-size: {config.font_size_px}px;\n"
            f"  line-height: 1.1;\n"
            f"}}"
        )

        if config.shrink_to_fit:
            init_js = _build_datebanner_init_js_autofit(
                date_id=date_id,
                options=options,
                prefix=config.prefix,
                uppercase=config.uppercase,
            )
            return WidgetRender(
                html=html_out,
                css=css_out,
                js=AUTOFIT_JS,
                init_js=init_js,
            )

        init_js = _build_datebanner_init_js(
            date_id=date_id,
            options=options,
            prefix=config.prefix,
            uppercase=config.uppercase,
        )

        return WidgetRender(
            html=html_out,
            css=css_out,
            init_js=init_js,
        )
