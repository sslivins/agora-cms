"""Analog clock widget — instance-scoped SVG face with live hands.

Pure-JS, no asset dependencies.  Renders a fixed ``0 0 100 100`` SVG
viewBox (so it scales cleanly to whatever cell it lands in) and rotates
three hand groups once a second via ``setInterval``.  Like the digital
:mod:`cms.composed.widgets.clock` widget it reads the browser ``Date``,
so the displayed time follows the device OS clock + TZ.

Instance scoping: every DOM ID + CSS class is suffixed with the widget
instance UUID so two analog clocks in the same bundle don't collide.
The hand groups carry stable per-instance IDs the init script looks up.

Determinism: the SVG markup + init script are a pure function of the
config + instance ID (no timestamps baked in at build time), so the
bundle builder's byte-identical-rebuild guarantee holds.  The only
runtime variability is the live clock, which is exactly the point.
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell

_HEX = r"^#[0-9a-fA-F]{6}$"


class AnalogClockWidgetConfig(BaseModel):
    """User-editable config for :class:`AnalogClockWidget`."""

    model_config = ConfigDict(extra="forbid")

    face_color: str = Field(default="#111827", pattern=_HEX)
    hand_color: str = Field(default="#ffffff", pattern=_HEX)
    second_color: str = Field(default="#ef4444", pattern=_HEX)
    show_seconds: bool = True
    show_ticks: bool = True
    show_numerals: bool = False


# Per-instance init script.  ``$NAME`` placeholders (not ``{name}``) so
# we don't have to brace-double every literal ``{`` in the JS body.
_ANALOG_INIT_JS_TEMPLATE = """
var hourEl = document.getElementById($HOUR_ID_LIT);
var minEl = document.getElementById($MIN_ID_LIT);
var secEl = $SEC_LOOKUP;
function rot(el, deg) {
  if (el) el.setAttribute('transform', 'rotate(' + deg + ' 50 50)');
}
function render() {
  var d = new Date();
  var s = d.getSeconds();
  var m = d.getMinutes();
  var h = d.getHours() % 12;
  // Smooth coupling: minutes feed the hour hand, seconds feed the
  // minute hand, so the dial reads accurately between ticks.
  rot(hourEl, (h * 30) + (m * 0.5));
  rot(minEl, (m * 6) + (s * 0.1));
  if (secEl) rot(secEl, s * 6);
}
render();
setInterval(render, 1000);
""".strip()


def _build_analog_init_js(*, hour_id: str, min_id: str, sec_id: str) -> str:
    return (
        _ANALOG_INIT_JS_TEMPLATE.replace("$HOUR_ID_LIT", f"'{hour_id}'")
        .replace("$MIN_ID_LIT", f"'{min_id}'")
        .replace(
            "$SEC_LOOKUP",
            f"document.getElementById('{sec_id}')" if sec_id else "null",
        )
    )


def _tick_marks(css_class: str, *, hour: bool) -> str:
    """Emit hour (or minute) tick ``<line>`` elements around the dial."""
    out: list[str] = []
    count = 12 if hour else 60
    for i in range(count):
        if hour and i % 1 != 0:  # always true for hour set; kept explicit
            continue
        if not hour and i % 5 == 0:
            continue  # skip positions that already carry an hour tick
        angle = i * (360 / count)
        cls = f"{css_class}-tick-major" if hour else f"{css_class}-tick-minor"
        # A vertical line near the top, rotated into place.
        y2 = 8 if hour else 6
        out.append(
            f'<line x1="50" y1="2" x2="50" y2="{y2}" '
            f'class="{cls}" transform="rotate({angle:.4f} 50 50)"/>'
        )
    return "".join(out)


def _numerals(css_class: str) -> str:
    """Emit 1–12 numerals positioned on a radius inside the dial."""
    import math

    out: list[str] = []
    radius = 38.0
    for n in range(1, 13):
        angle = math.radians(n * 30)
        x = 50 + radius * math.sin(angle)
        y = 50 - radius * math.cos(angle)
        out.append(
            f'<text x="{x:.3f}" y="{y:.3f}" class="{css_class}-num" '
            f'text-anchor="middle" dominant-baseline="central">{n}</text>'
        )
    return "".join(out)


class AnalogClockWidget(Widget):
    """Live analog wall-clock display (SVG)."""

    slug: ClassVar[str] = "analog_clock"
    display_name: ClassVar[str] = "Analog Clock"
    icon: ClassVar[str] = "🕧"
    ConfigSchema: ClassVar[type[BaseModel]] = AnalogClockWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        return {
            "face_color": "#111827",
            "hand_color": "#ffffff",
            "second_color": "#ef4444",
            "show_seconds": True,
            "show_ticks": True,
            "show_numerals": False,
        }

    def editor_template(self) -> str:
        return "composed/widgets/analog_clock.html"

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        del ctx  # no asset deps
        assert isinstance(config, AnalogClockWidgetConfig), (
            "AnalogClockWidget.render_html expects an AnalogClockWidgetConfig"
        )

        css_class = f"cw-aclock-{instance_id}"
        hour_id = f"cw-aclock-hour-{instance_id}"
        min_id = f"cw-aclock-min-{instance_id}"
        sec_id = f"cw-aclock-sec-{instance_id}"

        ticks_html = ""
        if config.show_ticks:
            ticks_html = _tick_marks(css_class, hour=True) + _tick_marks(
                css_class, hour=False
            )
        numerals_html = _numerals(css_class) if config.show_numerals else ""

        # Hands point straight up at rotation 0 (12 o'clock).  Each hand
        # is a group so the init script can rotate the whole group about
        # the dial centre (50,50).
        sec_hand = (
            f'<g id="{sec_id}"><line x1="50" y1="54" x2="50" y2="14" '
            f'class="{css_class}-sec"/></g>'
            if config.show_seconds
            else ""
        )
        html_out = (
            f'<div class="{css_class}">'
            f'<svg class="{css_class}-svg" viewBox="0 0 100 100" '
            f'preserveAspectRatio="xMidYMid meet">'
            f'<circle cx="50" cy="50" r="48" class="{css_class}-face"/>'
            f"{ticks_html}"
            f"{numerals_html}"
            f'<g id="{hour_id}"><line x1="50" y1="52" x2="50" y2="26" '
            f'class="{css_class}-hour"/></g>'
            f'<g id="{min_id}"><line x1="50" y1="53" x2="50" y2="16" '
            f'class="{css_class}-min"/></g>'
            f"{sec_hand}"
            f'<circle cx="50" cy="50" r="2.2" class="{css_class}-pin"/>'
            f"</svg>"
            f"</div>"
        )

        css_out = (
            f".{css_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: flex;\n"
            f"  align-items: center;\n"
            f"  justify-content: center;\n"
            f"}}\n"
            f".{css_class}-svg {{ width: 100%; height: 100%; }}\n"
            f".{css_class}-face {{\n"
            f"  fill: {config.face_color};\n"
            f"  stroke: {config.hand_color};\n"
            f"  stroke-width: 1;\n"
            f"  stroke-opacity: 0.35;\n"
            f"}}\n"
            f".{css_class}-tick-major {{ stroke: {config.hand_color}; "
            f"stroke-width: 1.4; }}\n"
            f".{css_class}-tick-minor {{ stroke: {config.hand_color}; "
            f"stroke-width: 0.5; stroke-opacity: 0.5; }}\n"
            f".{css_class}-num {{ fill: {config.hand_color}; "
            f"font-family: system-ui, sans-serif; font-size: 7px; }}\n"
            f".{css_class}-hour {{ stroke: {config.hand_color}; "
            f"stroke-width: 3; stroke-linecap: round; }}\n"
            f".{css_class}-min {{ stroke: {config.hand_color}; "
            f"stroke-width: 2; stroke-linecap: round; }}\n"
            f".{css_class}-sec {{ stroke: {config.second_color}; "
            f"stroke-width: 1; stroke-linecap: round; }}\n"
            f".{css_class}-pin {{ fill: {config.second_color}; }}"
        )

        init_js = _build_analog_init_js(
            hour_id=hour_id,
            min_id=min_id,
            sec_id=sec_id if config.show_seconds else "",
        )

        return WidgetRender(html=html_out, css=css_out, init_js=init_js)
