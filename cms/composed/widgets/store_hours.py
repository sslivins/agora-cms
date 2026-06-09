"""Store hours widget — shows weekly opening hours + live open/closed status.

Pure-JS, no asset dependencies and no network fetch.  The weekly
schedule, holiday overrides, colours and layout are all baked into the
config at author time.  At runtime a per-minute ``setInterval`` re-evaluates
the device-local clock against the schedule to flip an Open / Closed badge
and compute a "Closes 7:00 PM" / "Opens Mon 9:00 AM" hint.

Like :mod:`cms.composed.widgets.clock` and
:mod:`cms.composed.widgets.datebanner`, all time logic uses the browser's
``Date`` object, so the displayed state follows whatever the OS clock + TZ
are set to on the device (offline-safe, no IANA tz handling needed).

Two scheduling layers, both evaluated client-side:

* **Weekly schedule** — each weekday is a list of ``{open, close}``
  intervals (supports split / lunch hours, e.g. ``09:00-12:00`` +
  ``13:00-17:00``).  An empty list means closed that day.
* **Holiday overrides** — one-off (``YYYY-MM-DD``) or recurring annual
  (``MM-DD``) date matches that either close the store or replace that
  day's intervals with special hours.

Instance scoping: every DOM ID + CSS class is suffixed with the widget
instance UUID so two store-hours widgets in the same bundle don't collide.
"""

from __future__ import annotations

import html
import json
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell

# Font-family allowlist — kept local so typography can evolve independently.
_FONT_STACKS: dict[str, str] = {
    "sans": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "serif": "Georgia, Cambria, 'Times New Roman', serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
}

# JS-``getDay()`` order: index 0 == Sunday … 6 == Saturday.
_DAY_FIELDS_JS_ORDER: tuple[str, ...] = (
    "sunday",
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
)
_DAY_FULL_JS_ORDER: list[str] = [
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
]
_DAY_SHORT_JS_ORDER: list[str] = [
    "Sun",
    "Mon",
    "Tue",
    "Wed",
    "Thu",
    "Fri",
    "Sat",
]
# Display order for the week table — Monday first, Sunday last (JS indices).
_WEEK_TABLE_ORDER: list[int] = [1, 2, 3, 4, 5, 6, 0]


def _parse_hhmm(value: str) -> int:
    """Parse ``"HH:MM"`` → minutes since midnight.

    Hours may be ``0``-``24``; ``24:00`` (== 1440) is permitted only as a
    closing time meaning "midnight / end of day".
    """
    parts = value.split(":")
    if len(parts) != 2 or not parts[0].isdigit() or not parts[1].isdigit():
        raise ValueError(f"time must be 'HH:MM', got {value!r}")
    hours, minutes = int(parts[0]), int(parts[1])
    if not (0 <= hours <= 24) or not (0 <= minutes <= 59):
        raise ValueError(f"time out of range: {value!r}")
    total = hours * 60 + minutes
    if total > 1440:
        raise ValueError(f"time may not exceed 24:00, got {value!r}")
    return total


class Interval(BaseModel):
    """A single open→close span within a day."""

    model_config = ConfigDict(extra="forbid")

    open: str = Field(default="09:00")
    close: str = Field(default="17:00")

    @field_validator("open", "close")
    @classmethod
    def _valid_hhmm(cls, v: str) -> str:
        _parse_hhmm(v)
        return v

    @model_validator(mode="after")
    def _close_after_open(self) -> Interval:
        if _parse_hhmm(self.close) <= _parse_hhmm(self.open):
            raise ValueError(
                f"close ({self.close}) must be after open ({self.open})"
            )
        return self


def _validate_no_overlap(intervals: list[Interval]) -> list[Interval]:
    """Ensure a day's intervals don't overlap (order-independent)."""
    spans = sorted(
        ((_parse_hhmm(i.open), _parse_hhmm(i.close)) for i in intervals),
        key=lambda s: s[0],
    )
    for prev, cur in zip(spans, spans[1:]):
        if cur[0] < prev[1]:
            raise ValueError("intervals within a day must not overlap")
    return intervals


class HolidayOverride(BaseModel):
    """A date-specific override of the weekly schedule.

    ``date`` is either a one-off ``YYYY-MM-DD`` or a recurring annual
    ``MM-DD``.  ``closed=True`` ignores ``intervals``.
    """

    model_config = ConfigDict(extra="forbid")

    date: str
    label: str = Field(default="", max_length=80)
    closed: bool = True
    intervals: list[Interval] = Field(default_factory=list)

    @field_validator("date")
    @classmethod
    def _valid_date(cls, v: str) -> str:
        parts = v.split("-")
        if len(parts) == 3:
            y, m, d = parts
            if not (len(y) == 4 and y.isdigit()):
                raise ValueError("date year must be 4 digits (YYYY-MM-DD)")
        elif len(parts) == 2:
            m, d = parts
        else:
            raise ValueError("date must be 'YYYY-MM-DD' or 'MM-DD'")
        if not (m.isdigit() and d.isdigit() and 1 <= int(m) <= 12 and 1 <= int(d) <= 31):
            raise ValueError(f"invalid month/day in date: {v!r}")
        return v

    @model_validator(mode="after")
    def _check_intervals(self) -> HolidayOverride:
        if not self.closed:
            _validate_no_overlap(self.intervals)
        return self


def _default_week() -> dict[str, list[dict[str, str]]]:
    """Mon–Fri 9–5, Sat 10–4, Sun closed."""
    weekday = [{"open": "09:00", "close": "17:00"}]
    saturday = [{"open": "10:00", "close": "16:00"}]
    return {
        "monday": list(weekday),
        "tuesday": list(weekday),
        "wednesday": list(weekday),
        "thursday": list(weekday),
        "friday": list(weekday),
        "saturday": list(saturday),
        "sunday": [],
    }


class StoreHoursWidgetConfig(BaseModel):
    """User-editable config for :class:`StoreHoursWidget`."""

    model_config = ConfigDict(extra="forbid")

    monday: list[Interval] = Field(default_factory=list)
    tuesday: list[Interval] = Field(default_factory=list)
    wednesday: list[Interval] = Field(default_factory=list)
    thursday: list[Interval] = Field(default_factory=list)
    friday: list[Interval] = Field(default_factory=list)
    saturday: list[Interval] = Field(default_factory=list)
    sunday: list[Interval] = Field(default_factory=list)

    holidays: list[HolidayOverride] = Field(default_factory=list, max_length=60)

    display_mode: Literal["today", "week"] = "today"
    heading: str = Field(default="", max_length=80)
    show_status: bool = True
    time_format: Literal["12h", "24h"] = "12h"

    color: str = Field(default="#ffffff", pattern=r"^#[0-9a-fA-F]{6}$")
    open_color: str = Field(default="#3fb950", pattern=r"^#[0-9a-fA-F]{6}$")
    closed_color: str = Field(default="#f85149", pattern=r"^#[0-9a-fA-F]{6}$")
    font_family: str = Field(default="sans")
    font_size_px: int = Field(default=48, ge=8, le=512)

    @field_validator("font_family")
    @classmethod
    def _font_in_allowlist(cls, v: str) -> str:
        if v not in _FONT_STACKS:
            allowed = ", ".join(sorted(_FONT_STACKS))
            raise ValueError(f"font_family must be one of: {allowed}")
        return v

    @model_validator(mode="after")
    def _days_no_overlap(self) -> StoreHoursWidgetConfig:
        for field in _DAY_FIELDS_JS_ORDER:
            _validate_no_overlap(getattr(self, field))
        return self


def _intervals_js(intervals: list[Interval]) -> str:
    """``[[540,1020], ...]`` minute-pair literal for the runtime."""
    pairs = ",".join(
        f"[{_parse_hhmm(i.open)},{_parse_hhmm(i.close)}]" for i in intervals
    )
    return f"[{pairs}]"


def _schedule_js(config: StoreHoursWidgetConfig) -> str:
    """Weekly schedule as a JS array indexed by ``getDay()``."""
    days = ",".join(
        _intervals_js(getattr(config, field)) for field in _DAY_FIELDS_JS_ORDER
    )
    return f"[{days}]"


def _holidays_js(config: StoreHoursWidgetConfig) -> str:
    entries = ",".join(
        "{{match:{m},label:{l},closed:{c},intervals:{iv}}}".format(
            m=json.dumps(h.date),
            l=json.dumps(h.label),
            c="true" if h.closed else "false",
            iv=_intervals_js(h.intervals),
        )
        for h in config.holidays
    )
    return f"[{entries}]"


# ``$NAME`` placeholders (not ``{name}``) so literal ``{`` in the JS body
# don't need brace-doubling.
_STOREHOURS_INIT_JS_TEMPLATE = """
var statusEl = document.getElementById($STATUS_ID);
var bodyEl = document.getElementById($BODY_ID);
var SCHEDULE = $SCHEDULE;
var HOLIDAYS = $HOLIDAYS;
var DAY_FULL = $DAY_FULL;
var DAY_SHORT = $DAY_SHORT;
var WEEK_ORDER = $WEEK_ORDER;
var MODE = $MODE;
var SHOW_STATUS = $SHOW_STATUS;
var FMT24 = $FMT24;
var OPEN_COLOR = $OPEN_COLOR;
var CLOSED_COLOR = $CLOSED_COLOR;

function pad2(n) { return (n < 10 ? '0' : '') + n; }

function fmtTime(mins) {
  if (FMT24) { return mins >= 1440 ? '24:00' : pad2(Math.floor(mins / 60)) + ':' + pad2(mins % 60); }
  if (mins >= 1440) return '12:00 AM';
  var h = Math.floor(mins / 60), m = mins % 60;
  var suf = h >= 12 ? 'PM' : 'AM';
  var hh = h % 12; if (hh === 0) hh = 12;
  return hh + ':' + pad2(m) + ' ' + suf;
}

function fmtSpan(iv) { return fmtTime(iv[0]) + ' \u2013 ' + fmtTime(iv[1]); }
function fmtDay(ivs) { return ivs.length ? ivs.map(fmtSpan).join(', ') : 'Closed'; }

function holidayFor(date) {
  var mmdd = pad2(date.getMonth() + 1) + '-' + pad2(date.getDate());
  var ymd = date.getFullYear() + '-' + mmdd;
  for (var i = 0; i < HOLIDAYS.length; i++) {
    if (HOLIDAYS[i].match === ymd || HOLIDAYS[i].match === mmdd) return HOLIDAYS[i];
  }
  return null;
}

function intervalsFor(date) {
  var h = holidayFor(date);
  if (h) return h.closed ? [] : h.intervals;
  return SCHEDULE[date.getDay()] || [];
}

function renderStatus(now) {
  if (!statusEl) return;
  while (statusEl.firstChild) statusEl.removeChild(statusEl.firstChild);
  var nowMin = now.getHours() * 60 + now.getMinutes();
  var today = intervalsFor(now);
  var openNow = false, closesAt = null;
  for (var i = 0; i < today.length; i++) {
    if (nowMin >= today[i][0] && nowMin < today[i][1]) { openNow = true; closesAt = today[i][1]; break; }
  }
  var pill = document.createElement('span');
  pill.className = 'cw-storehours-pill';
  var hint = document.createElement('span');
  hint.className = 'cw-storehours-hint';
  var hol = holidayFor(now);
  if (openNow) {
    pill.textContent = 'Open';
    pill.style.color = OPEN_COLOR;
    hint.textContent = ' \u00b7 Closes ' + fmtTime(closesAt);
  } else {
    pill.textContent = 'Closed';
    pill.style.color = CLOSED_COLOR;
    var next = null;
    for (var j = 0; j < today.length; j++) {
      if (today[j][0] > nowMin) { next = { off: 0, min: today[j][0] }; break; }
    }
    if (!next) {
      for (var off = 1; off <= 7; off++) {
        var d = new Date(now.getTime() + off * 86400000);
        var ivs = intervalsFor(d);
        if (ivs.length) { next = { off: off, min: ivs[0][0], day: d.getDay() }; break; }
      }
    }
    if (next && next.off === 0) {
      hint.textContent = ' \u00b7 Opens ' + fmtTime(next.min);
    } else if (next) {
      hint.textContent = ' \u00b7 Opens ' + DAY_SHORT[next.day] + ' ' + fmtTime(next.min);
    } else if (hol && hol.label) {
      hint.textContent = ' \u00b7 ' + hol.label;
    }
  }
  if (hol && hol.label && pill.textContent === 'Closed' && !hint.textContent) {
    hint.textContent = ' \u00b7 ' + hol.label;
  }
  statusEl.appendChild(pill);
  statusEl.appendChild(hint);
}

function renderBody(now) {
  if (!bodyEl) return;
  while (bodyEl.firstChild) bodyEl.removeChild(bodyEl.firstChild);
  if (MODE === 'week') {
    for (var k = 0; k < WEEK_ORDER.length; k++) {
      var idx = WEEK_ORDER[k];
      var row = document.createElement('div');
      row.className = 'cw-storehours-row' + (idx === now.getDay() ? ' cw-storehours-row-today' : '');
      var name = document.createElement('span');
      name.className = 'cw-storehours-day';
      name.textContent = DAY_FULL[idx];
      var hrs = document.createElement('span');
      hrs.className = 'cw-storehours-hrs';
      hrs.textContent = fmtDay(SCHEDULE[idx] || []);
      row.appendChild(name);
      row.appendChild(hrs);
      bodyEl.appendChild(row);
    }
  } else {
    var line = document.createElement('div');
    line.className = 'cw-storehours-today';
    line.textContent = 'Today: ' + fmtDay(intervalsFor(now));
    bodyEl.appendChild(line);
  }
}

function render() {
  var now = new Date();
  if (SHOW_STATUS) renderStatus(now);
  renderBody(now);
}
render();
setInterval(render, 60000);
""".strip()


def _build_storehours_init_js(*, status_id: str, body_id: str, config: StoreHoursWidgetConfig) -> str:
    return (
        _STOREHOURS_INIT_JS_TEMPLATE
        .replace("$STATUS_ID", f"'{status_id}'")
        .replace("$BODY_ID", f"'{body_id}'")
        .replace("$SCHEDULE", _schedule_js(config))
        .replace("$HOLIDAYS", _holidays_js(config))
        .replace("$DAY_FULL", json.dumps(_DAY_FULL_JS_ORDER))
        .replace("$DAY_SHORT", json.dumps(_DAY_SHORT_JS_ORDER))
        .replace("$WEEK_ORDER", json.dumps(_WEEK_TABLE_ORDER))
        .replace("$MODE", json.dumps(config.display_mode))
        .replace("$SHOW_STATUS", "true" if config.show_status else "false")
        .replace("$FMT24", "true" if config.time_format == "24h" else "false")
        .replace("$OPEN_COLOR", json.dumps(config.open_color))
        .replace("$CLOSED_COLOR", json.dumps(config.closed_color))
    )


class StoreHoursWidget(Widget):
    """Weekly store hours + live open/closed status."""

    slug: ClassVar[str] = "storehours"
    display_name: ClassVar[str] = "Store Hours"
    icon: ClassVar[str] = "🕗"
    ConfigSchema: ClassVar[type[BaseModel]] = StoreHoursWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        week = _default_week()
        return {
            **week,
            "holidays": [],
            "display_mode": "today",
            "heading": "",
            "show_status": True,
            "time_format": "12h",
            "color": "#ffffff",
            "open_color": "#3fb950",
            "closed_color": "#f85149",
            "font_family": "sans",
            "font_size_px": 48,
        }

    def editor_template(self) -> str:
        return "composed/widgets/store_hours.html"

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        # No asset deps; ctx + cell unused.
        del ctx, cell
        assert isinstance(config, StoreHoursWidgetConfig), (
            "StoreHoursWidget.render_html expects a StoreHoursWidgetConfig"
        )

        css_class = f"cw-storehours-{instance_id}"
        status_id = f"cw-storehours-status-{instance_id}"
        body_id = f"cw-storehours-body-{instance_id}"
        font_stack = _FONT_STACKS[config.font_family]

        heading_html = ""
        if config.heading:
            heading_html = (
                f'<div class="{css_class}-heading">{html.escape(config.heading)}</div>'
            )
        status_html = ""
        if config.show_status:
            status_html = f'<div id="{status_id}" class="{css_class}-status">\u2026</div>'

        html_out = (
            f'<div class="{css_class}">'
            f"{heading_html}"
            f"{status_html}"
            f'<div id="{body_id}" class="{css_class}-body"></div>'
            f"</div>"
        )

        css_out = (
            f".{css_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  box-sizing: border-box;\n"
            f"  display: flex;\n"
            f"  flex-direction: column;\n"
            f"  align-items: center;\n"
            f"  justify-content: center;\n"
            f"  overflow: hidden;\n"
            f"  text-align: center;\n"
            f"  color: {config.color};\n"
            f"  font-family: {font_stack};\n"
            f"  font-size: {config.font_size_px}px;\n"
            f"  line-height: 1.25;\n"
            f"}}\n"
            f".{css_class}-heading {{\n"
            f"  font-weight: 700;\n"
            f"  margin-bottom: 0.25em;\n"
            f"}}\n"
            f".{css_class}-status {{\n"
            f"  margin-bottom: 0.35em;\n"
            f"}}\n"
            f".{css_class}-status .cw-storehours-pill {{\n"
            f"  font-weight: 700;\n"
            f"}}\n"
            f".{css_class}-status .cw-storehours-hint {{\n"
            f"  opacity: 0.85;\n"
            f"}}\n"
            f".{css_class}-body {{\n"
            f"  width: 100%;\n"
            f"  display: flex;\n"
            f"  flex-direction: column;\n"
            f"  gap: 0.15em;\n"
            f"}}\n"
            f".{css_class}-body .cw-storehours-row {{\n"
            f"  display: flex;\n"
            f"  justify-content: space-between;\n"
            f"  gap: 1em;\n"
            f"  white-space: nowrap;\n"
            f"}}\n"
            f".{css_class}-body .cw-storehours-row-today {{\n"
            f"  font-weight: 700;\n"
            f"}}\n"
            f".{css_class}-body .cw-storehours-day {{\n"
            f"  text-align: left;\n"
            f"}}\n"
            f".{css_class}-body .cw-storehours-hrs {{\n"
            f"  text-align: right;\n"
            f"}}"
        )

        init_js = _build_storehours_init_js(
            status_id=status_id,
            body_id=body_id,
            config=config,
        )

        return WidgetRender(
            html=html_out,
            css=css_out,
            init_js=init_js,
        )
