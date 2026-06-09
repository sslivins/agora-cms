"""Countdown / count-up timer widget — pure-JS, no asset dependencies.

Counts **down** to a target wall-clock moment (a sale ending, an event
starting) or **up** from one (days since a milestone).  Like the clock
widget it reads the device's local ``Date`` so the displayed value
follows the OS clock + timezone — intentional for v1 (no per-slide TZ
metadata yet).

The target moment is a **naive local datetime** (``YYYY-MM-DDTHH:MM``,
from the editor's ``datetime-local`` input).  It is baked into the init
script as a ``new Date(y, mo-1, d, h, mi, s)`` constructor so the device
interprets it in its own local time — deterministic, offline-tolerant,
and byte-identical across rebuilds.

Unit selection: any combination of days / hours / minutes / seconds may
be shown.  The highest *enabled* unit absorbs the overflow of any larger
disabled units (e.g. with only hours+minutes shown, a 2-day remainder
renders as ``52h 00m``).  At least one unit must be enabled.

Security: the only config value interpolated into emitted JS is
``completed_text``, and it is injected as a JSON-encoded string literal
(``json.dumps``) so it can never break out of its literal.  The optional
``label`` lives in HTML and is escaped there.  Instance scoping: every
DOM ID + CSS class is suffixed with the widget instance UUID.
"""

from __future__ import annotations

import html
import json
from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell

# Font-family allowlist — kept local so each widget evolves typography
# independently (mirrors clock.py).
_FONT_STACKS: dict[str, str] = {
    "sans": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "serif": "Georgia, Cambria, 'Times New Roman', serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
}


class CountdownWidgetConfig(BaseModel):
    """User-editable config for :class:`CountdownWidget`."""

    model_config = ConfigDict(extra="forbid")

    # Naive local datetime, e.g. "2030-01-01T00:00" or with seconds.
    target: str = Field(default="2030-01-01T00:00:00")
    direction: Literal["down", "up"] = "down"
    label: str = Field(default="", max_length=120)
    completed_text: str = Field(default="", max_length=120)
    show_days: bool = True
    show_hours: bool = True
    show_minutes: bool = True
    show_seconds: bool = False
    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    font_family: str = Field(default="sans")
    font_size_px: int = Field(default=96, ge=8, le=512)

    @field_validator("font_family")
    @classmethod
    def _font_in_allowlist(cls, v: str) -> str:
        if v not in _FONT_STACKS:
            allowed = ", ".join(sorted(_FONT_STACKS))
            raise ValueError(f"font_family must be one of: {allowed}")
        return v

    @field_validator("target")
    @classmethod
    def _target_is_naive_local_datetime(cls, v: str) -> str:
        try:
            parsed = datetime.fromisoformat(v)
        except ValueError as exc:
            raise ValueError(
                "target must be an ISO 8601 datetime (YYYY-MM-DDTHH:MM)"
            ) from exc
        if parsed.tzinfo is not None:
            raise ValueError(
                "target must be a naive local datetime (no timezone offset)"
            )
        return v

    @model_validator(mode="after")
    def _at_least_one_unit(self) -> CountdownWidgetConfig:
        if not (
            self.show_days
            or self.show_hours
            or self.show_minutes
            or self.show_seconds
        ):
            raise ValueError("at least one time unit must be shown")
        return self


# Per-instance init script.  Placeholders use ``$NAME`` rather than
# ``{name}`` so we don't have to brace-double the JS body (see clock.py).
_COUNTDOWN_INIT_JS_TEMPLATE = """
var timeEl = document.getElementById($TIME_ID_LIT);
var targetMs = $TARGET_EXPR;
var dir = '$DIRECTION';
var completed = $COMPLETED_LIT;
var defs = [
  [$SHOW_DAYS, 86400, 'd'],
  [$SHOW_HOURS, 3600, 'h'],
  [$SHOW_MINUTES, 60, 'm'],
  [$SHOW_SECONDS, 1, 's']
];
var intervalId = null;
function pad2(n) { return n < 10 ? '0' + n : '' + n; }
function render() {
  var now = Date.now();
  var diff = dir === 'down' ? (targetMs - now) : (now - targetMs);
  var done = false;
  if (diff <= 0) {
    if (dir === 'down') { done = true; }
    diff = 0;
  }
  if (done && completed.length > 0) {
    if (timeEl) { timeEl.textContent = completed; }
    if (intervalId !== null) { clearInterval(intervalId); intervalId = null; }
    return;
  }
  var rem = Math.floor(diff / 1000);
  var parts = [];
  var leading = true;
  for (var i = 0; i < defs.length; i++) {
    if (!defs[i][0]) { continue; }
    var unitSec = defs[i][1];
    var val = Math.floor(rem / unitSec);
    rem = rem - val * unitSec;
    parts.push((leading ? ('' + val) : pad2(val)) + defs[i][2]);
    leading = false;
  }
  if (timeEl) { timeEl.textContent = parts.join(' '); }
}
render();
intervalId = setInterval(render, 1000);
""".strip()


def _target_expr(target: str) -> str:
    """Bake the naive target into a device-local ``new Date(...)`` call."""
    d = datetime.fromisoformat(target)
    return (
        f"new Date({d.year}, {d.month - 1}, {d.day}, "
        f"{d.hour}, {d.minute}, {d.second}).getTime()"
    )


def _build_countdown_init_js(
    *,
    time_id: str,
    target: str,
    direction: str,
    completed_text: str,
    show_days: bool,
    show_hours: bool,
    show_minutes: bool,
    show_seconds: bool,
) -> str:
    def _b(flag: bool) -> str:
        return "true" if flag else "false"

    return (
        _COUNTDOWN_INIT_JS_TEMPLATE
        .replace("$TIME_ID_LIT", f"'{time_id}'")
        .replace("$TARGET_EXPR", _target_expr(target))
        .replace("$DIRECTION", direction)
        .replace("$COMPLETED_LIT", json.dumps(completed_text))
        .replace("$SHOW_DAYS", _b(show_days))
        .replace("$SHOW_HOURS", _b(show_hours))
        .replace("$SHOW_MINUTES", _b(show_minutes))
        .replace("$SHOW_SECONDS", _b(show_seconds))
    )


class CountdownWidget(Widget):
    """Live countdown-to / count-up-from a target moment."""

    slug: ClassVar[str] = "countdown"
    display_name: ClassVar[str] = "Countdown"
    icon: ClassVar[str] = "⏳"
    ConfigSchema: ClassVar[type[BaseModel]] = CountdownWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        return {
            "target": "2030-01-01T00:00:00",
            "direction": "down",
            "label": "",
            "completed_text": "",
            "show_days": True,
            "show_hours": True,
            "show_minutes": True,
            "show_seconds": False,
            "color": "#ffffff",
            "font_family": "sans",
            "font_size_px": 96,
        }

    def editor_template(self) -> str:
        return "composed/widgets/countdown.html"

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        # Countdown has no asset deps; ctx/cell are ignored.
        del ctx, cell
        assert isinstance(config, CountdownWidgetConfig), (
            "CountdownWidget.render_html expects a CountdownWidgetConfig instance"
        )

        css_class = f"cw-countdown-{instance_id}"
        time_id = f"cw-countdown-time-{instance_id}"
        font_stack = _FONT_STACKS[config.font_family]

        label_html = (
            f'<div class="{css_class}-label">{html.escape(config.label)}</div>'
            if config.label
            else ""
        )
        html_out = (
            f'<div class="{css_class}">'
            f"{label_html}"
            f'<div id="{time_id}" class="{css_class}-time"></div>'
            f"</div>"
        )

        # Subordinate label font sized at 40% of the timer font (matches
        # the clock widget's date treatment).
        label_size = max(8, int(config.font_size_px * 0.4))

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
            f".{css_class}-label {{\n"
            f"  font-size: {label_size}px;\n"
            f"  opacity: 0.8;\n"
            f"  margin-bottom: 0.2em;\n"
            f"}}\n"
            f".{css_class}-time {{\n"
            f"  font-size: {config.font_size_px}px;\n"
            f"  line-height: 1;\n"
            f"  white-space: nowrap;\n"
            f"}}"
        )

        init_js = _build_countdown_init_js(
            time_id=time_id,
            target=config.target,
            direction=config.direction,
            completed_text=config.completed_text,
            show_days=config.show_days,
            show_hours=config.show_hours,
            show_minutes=config.show_minutes,
            show_seconds=config.show_seconds,
        )

        return WidgetRender(html=html_out, css=css_out, init_js=init_js)
