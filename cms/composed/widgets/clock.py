"""Clock widget — wall-clock display using the device's local time.

Pure-JS, no asset dependencies.  Updates every second via
``setInterval``.  Uses the browser's ``Date`` object so the displayed
time follows whatever the OS clock + TZ are set to on the device —
intentional for Phase 1B (no CMS-side TZ config yet; per-device TZ
becomes meaningful in Phase 5+ when slides start carrying timezone
metadata).

Instance scoping: every DOM ID + CSS class is suffixed with the
widget instance UUID so two clock widgets in the same bundle don't
collide.  The init script captures ``instanceId`` from the per-init
closure the bundle builder wraps each ``init_js`` block in
(see :mod:`cms.composed.bundle`).
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell
from cms.composed.widgets._autofit import AUTOFIT_JS, autofit_inner_init_js


# Font-family allowlist — kept local to this widget so the clock and
# text widget can evolve typography independently.
_FONT_STACKS: dict[str, str] = {
    "sans": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "serif": "Georgia, Cambria, 'Times New Roman', serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
}


class ClockWidgetConfig(BaseModel):
    """User-editable config for :class:`ClockWidget`."""

    model_config = ConfigDict(extra="forbid")

    format: Literal["12h", "24h"] = "24h"
    show_seconds: bool = True
    show_date: bool = False
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    font_family: str = Field(default="sans")
    font_size_px: int = Field(default=96, ge=8, le=512)
    shrink_to_fit: bool = False

    @field_validator("font_family")
    @classmethod
    def _font_in_allowlist(cls, v: str) -> str:
        if v not in _FONT_STACKS:
            allowed = ", ".join(sorted(_FONT_STACKS))
            raise ValueError(f"font_family must be one of: {allowed}")
        return v


# Per-instance init script.  Placeholders use ``$NAME`` rather than
# ``{name}`` so we don't have to brace-double every literal ``{`` in
# the JS body (much easier to read + edit).
_CLOCK_INIT_JS_TEMPLATE = """
var timeEl = document.getElementById($TIME_ID_LIT);
var dateEl = $DATE_LOOKUP;
function pad2(n) { return n < 10 ? '0' + n : '' + n; }
function render() {
  var d = new Date();
  var hours = d.getHours();
  var minutes = d.getMinutes();
  var seconds = d.getSeconds();
  var suffix = '';
  if ('$FORMAT' === '12h') {
    suffix = hours >= 12 ? ' PM' : ' AM';
    hours = hours % 12;
    if (hours === 0) hours = 12;
  }
  var t = ('$FORMAT' === '24h' ? pad2(hours) : ('' + hours)) + ':' + pad2(minutes);
  if ($SHOW_SECONDS) t += ':' + pad2(seconds);
  t += suffix;
  if (timeEl) timeEl.textContent = t;
  if (dateEl && $SHOW_DATE) {
    dateEl.textContent = d.toLocaleDateString(undefined, {
      weekday: 'long', year: 'numeric', month: 'long', day: 'numeric'
    });
  }
}
render();
setInterval(render, 1000);
""".strip()


def _build_clock_init_js(
    *,
    time_id: str,
    date_id: str,
    format_: str,
    show_seconds: bool,
    show_date: bool,
) -> str:
    return (
        _CLOCK_INIT_JS_TEMPLATE
        .replace("$TIME_ID_LIT", f"'{time_id}'")
        .replace(
            "$DATE_LOOKUP",
            f"document.getElementById('{date_id}')" if date_id else "null",
        )
        .replace("$FORMAT", format_)
        .replace("$SHOW_SECONDS", "true" if show_seconds else "false")
        .replace("$SHOW_DATE", "true" if show_date else "false")
    )


class ClockWidget(Widget):
    """Live wall-clock display."""

    slug: ClassVar[str] = "clock"
    display_name: ClassVar[str] = "Clock"
    icon: ClassVar[str] = "🕒"
    ConfigSchema: ClassVar[type[BaseModel]] = ClockWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        return {
            "format": "24h",
            "show_seconds": True,
            "show_date": False,
            "color": "#ffffff",
            "font_family": "sans",
            "font_size_px": 96,
            "shrink_to_fit": False,
        }

    def editor_template(self) -> str:
        return "composed/widgets/clock.html"

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        # Clock has no asset deps; ctx is ignored.
        del ctx
        assert isinstance(config, ClockWidgetConfig), (
            "ClockWidget.render_html expects a ClockWidgetConfig instance"
        )

        css_class = f"cw-clock-{instance_id}"
        time_id = f"cw-clock-time-{instance_id}"
        date_id = f"cw-clock-date-{instance_id}"
        font_stack = _FONT_STACKS[config.font_family]

        if config.shrink_to_fit:
            return self._render_shrink(
                config=config,
                css_class=css_class,
                time_id=time_id,
                date_id=date_id,
                font_stack=font_stack,
                instance_id=instance_id,
            )

        date_html = (
            f'<div id="{date_id}" class="{css_class}-date"></div>'
            if config.show_date
            else ""
        )
        html_out = (
            f'<div class="{css_class}">'
            f'<div id="{time_id}" class="{css_class}-time"></div>'
            f"{date_html}"
            f"</div>"
        )

        # Subordinate date font sized at 40% of the time font.  Picked
        # by eye in the 1B mocks; reads cleanly on 1080p preview.
        date_size = max(8, int(config.font_size_px * 0.4))

        css_out = (
            f".{css_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: flex;\n"
            f"  flex-direction: column;\n"
            f"  align-items: center;\n"
            f"  justify-content: center;\n"
            f"  color: {config.color};\n"
            f"  font-family: {font_stack};\n"
            f"  font-variant-numeric: tabular-nums;\n"
            f"}}\n"
            f".{css_class}-time {{\n"
            f"  font-size: {config.font_size_px}px;\n"
            f"  line-height: 1;\n"
            f"}}\n"
            f".{css_class}-date {{\n"
            f"  font-size: {date_size}px;\n"
            f"  opacity: 0.8;\n"
            f"  margin-top: 0.25em;\n"
            f"}}"
        )

        init_js = _build_clock_init_js(
            time_id=time_id,
            date_id=date_id if config.show_date else "",
            format_=config.format,
            show_seconds=config.show_seconds,
            show_date=config.show_date,
        )

        return WidgetRender(
            html=html_out,
            css=css_out,
            init_js=init_js,
        )

    def _render_shrink(
        self,
        *,
        config: ClockWidgetConfig,
        css_class: str,
        time_id: str,
        date_id: str,
        font_stack: str,
        instance_id: str,
    ) -> WidgetRender:
        """Shrink-to-fit variant: the time (+ optional date) auto-scales.

        The time + date are wrapped in ``#cw-clock-inner-{id}`` which
        carries the base font size; child sizes are expressed in ``em`` so
        the whole composite scales proportionally when the shared autofit
        JS fits the wrapper against the bounded outer box.  The existing
        per-second render loop keeps writing the same child IDs; the
        ``MutationObserver`` inside ``__cwFitObserve`` re-fits on each tick
        for free.
        """
        inner_id = f"cw-clock-inner-{instance_id}"
        inner_class = f"{css_class}-inner"

        date_html = (
            f'<div id="{date_id}" class="{css_class}-date"></div>'
            if config.show_date
            else ""
        )
        html_out = (
            f'<div class="{css_class}">'
            f'<div id="{inner_id}" class="{inner_class}">'
            f'<div id="{time_id}" class="{css_class}-time"></div>'
            f"{date_html}"
            f"</div>"
            f"</div>"
        )

        css_out = (
            f".{css_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: flex;\n"
            f"  align-items: center;\n"
            f"  justify-content: center;\n"
            f"  color: {config.color};\n"
            f"  font-family: {font_stack};\n"
            f"  font-variant-numeric: tabular-nums;\n"
            f"  overflow: hidden;\n"
            f"}}\n"
            f".{inner_class} {{\n"
            # Starting size before JS runs; immediately overridden by fit.
            f"  font-size: {config.font_size_px}px;\n"
            f"  display: flex;\n"
            f"  flex-direction: column;\n"
            f"  align-items: center;\n"
            f"  justify-content: center;\n"
            f"}}\n"
            f".{css_class}-time {{\n"
            f"  font-size: 1em;\n"
            f"  line-height: 1;\n"
            f"}}\n"
            f".{css_class}-date {{\n"
            f"  font-size: 0.4em;\n"
            f"  opacity: 0.8;\n"
            f"  margin-top: 0.25em;\n"
            f"}}"
        )

        init_js = (
            _build_clock_init_js(
                time_id=time_id,
                date_id=date_id if config.show_date else "",
                format_=config.format,
                show_seconds=config.show_seconds,
                show_date=config.show_date,
            )
            + "\n"
            + autofit_inner_init_js(inner_id)
        )

        return WidgetRender(
            html=html_out,
            css=css_out,
            js=AUTOFIT_JS,
            init_js=init_js,
        )
