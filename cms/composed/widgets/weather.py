"""Weather widget — live current conditions from Open-Meteo.

Unlike every other v1 widget this one makes a **runtime network call**
from the device: its ``init_js`` fetches current temperature + a WMO
weather code from ``api.open-meteo.com`` (no API key required) and
paints them into the cell.  Coordinates are *baked in at build time*
(the editor geocodes a city name → lat/long via Open-Meteo's geocoding
API and stores the result in config), so the device never needs the
geocoding endpoint — only the lightweight forecast endpoint.

This is a deliberate, bounded exception to the bundle's
"offline-tolerant, zero-external-reference" property:

* The fetch is wrapped in an ``AbortController`` timeout and a
  ``try/catch`` so a network failure never throws — it falls back to a
  ``localStorage`` cache (keyed by a config fingerprint) and, failing
  that, a static "Weather unavailable" message.  The slide keeps
  playing regardless.
* The bundle's static markup still contains **no** ``src=`` / ``href=``
  external references — the URL only exists as a JS string literal, so
  the bundle's no-external-reference invariant (which only constrains
  HTML attributes) is preserved.
* The location label is rendered **server-side** with ``html.escape``;
  no config-controlled string is ever interpolated into the emitted
  JavaScript, so there's no script-injection surface here.

Instance scoping: every DOM ID + CSS class is suffixed with the widget
instance UUID.  The cache key is instance-scoped *and* carries a
``{lat,lon,units}`` fingerprint so changing the location or units never
shows stale cached weather.

Determinism: the emitted HTML/CSS/JS is a pure function of config +
instance ID.  The only non-deterministic runtime element is
``Math.random()`` jitter on the refresh timer, which lives in the
constant JS *source* — so byte-identical rebuilds still hold.
"""

from __future__ import annotations

import html
import json
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell
from cms.composed.widgets._autofit import AUTOFIT_JS, autofit_inner_init_js

_HEX = r"^#[0-9a-fA-F]{6}$"

_FONT_STACKS: dict[str, str] = {
    "sans": "system-ui, -apple-system, 'Segoe UI', Roboto, sans-serif",
    "serif": "Georgia, Cambria, 'Times New Roman', serif",
    "mono": "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace",
}

# Default location so a freshly-dropped widget is immediately a *valid*
# config (extra="forbid" + required coords would otherwise make a
# just-placed widget fail save/publish validation before the user picks
# a city).  Seattle — matches the deployment's locale; users change it
# via the editor's city search.
_DEFAULT_LAT = 47.6062
_DEFAULT_LON = -122.3321
_DEFAULT_LABEL = "Seattle"


def _js_str(s: str) -> str:
    """Serialise a Python string as a JS string literal, HTML-safe.

    ``json.dumps`` handles quote/backslash/control-char escaping; we
    additionally escape ``<>&`` so a stray ``</script>`` can never
    terminate the embedded script block.  (In practice the only values
    passed here are URLs / fingerprints / UUIDs, but stay defensive.)
    """
    return (
        json.dumps(s)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


class WeatherWidgetConfig(BaseModel):
    """User-editable config for :class:`WeatherWidget`."""

    model_config = ConfigDict(extra="forbid")

    latitude: float = Field(default=_DEFAULT_LAT, ge=-90.0, le=90.0)
    longitude: float = Field(default=_DEFAULT_LON, ge=-180.0, le=180.0)
    location_label: str = Field(default=_DEFAULT_LABEL, max_length=80)
    units: Literal["imperial", "metric"] = "imperial"
    show_condition: bool = True
    show_location: bool = True
    color: str = Field(default="#ffffff", pattern=_HEX)
    font_family: str = Field(default="sans")
    font_size_px: int = Field(default=64, ge=8, le=512)
    # Open-Meteo asks for a courteous refresh interval; current
    # conditions update at most a few times an hour upstream, so a
    # 10-minute floor is plenty and keeps us a good citizen.
    refresh_seconds: int = Field(default=900, ge=600, le=86400)
    shrink_to_fit: bool = False

    @field_validator("font_family")
    @classmethod
    def _font_in_allowlist(cls, v: str) -> str:
        if v not in _FONT_STACKS:
            allowed = ", ".join(sorted(_FONT_STACKS))
            raise ValueError(f"font_family must be one of: {allowed}")
        return v


# WMO weather interpretation codes → (label, emoji).  Compact map of
# the documented codes; unknown codes fall back to a neutral label at
# runtime so an upstream addition never breaks rendering.
# https://open-meteo.com/en/docs (WMO Weather interpretation codes)
_WMO: dict[int, tuple[str, str]] = {
    0: ("Clear", "\u2600\ufe0f"),
    1: ("Mainly clear", "\U0001f324\ufe0f"),
    2: ("Partly cloudy", "\u26c5"),
    3: ("Overcast", "\u2601\ufe0f"),
    45: ("Fog", "\U0001f32b\ufe0f"),
    48: ("Rime fog", "\U0001f32b\ufe0f"),
    51: ("Light drizzle", "\U0001f326\ufe0f"),
    53: ("Drizzle", "\U0001f326\ufe0f"),
    55: ("Heavy drizzle", "\U0001f326\ufe0f"),
    56: ("Freezing drizzle", "\U0001f327\ufe0f"),
    57: ("Freezing drizzle", "\U0001f327\ufe0f"),
    61: ("Light rain", "\U0001f327\ufe0f"),
    63: ("Rain", "\U0001f327\ufe0f"),
    65: ("Heavy rain", "\U0001f327\ufe0f"),
    66: ("Freezing rain", "\U0001f327\ufe0f"),
    67: ("Freezing rain", "\U0001f327\ufe0f"),
    71: ("Light snow", "\u2744\ufe0f"),
    73: ("Snow", "\u2744\ufe0f"),
    75: ("Heavy snow", "\u2744\ufe0f"),
    77: ("Snow grains", "\u2744\ufe0f"),
    80: ("Rain showers", "\U0001f326\ufe0f"),
    81: ("Rain showers", "\U0001f326\ufe0f"),
    82: ("Violent rain showers", "\u26c8\ufe0f"),
    85: ("Snow showers", "\U0001f328\ufe0f"),
    86: ("Snow showers", "\U0001f328\ufe0f"),
    95: ("Thunderstorm", "\u26c8\ufe0f"),
    96: ("Thunderstorm w/ hail", "\u26c8\ufe0f"),
    99: ("Thunderstorm w/ hail", "\u26c8\ufe0f"),
}


def _wmo_js_object() -> str:
    """Render :data:`_WMO` as a deterministic JS object literal."""
    parts: list[str] = []
    for code in sorted(_WMO):
        label, emoji = _WMO[code]
        parts.append(f"{code}:{{l:{_js_str(label)},e:{_js_str(emoji)}}}")
    return "{" + ",".join(parts) + "}"


_WEATHER_INIT_JS_TEMPLATE = """
var root = document.getElementById($ROOT_ID_LIT);
var tempEl = document.getElementById($TEMP_ID_LIT);
var condEl = $COND_LOOKUP;
var FORECAST_URL = $URL_LIT;
var SYMBOL = $SYMBOL_LIT;
var CFG_FP = $CFG_FP_LIT;
var CACHE_KEY = $CACHE_KEY_LIT;
var REFRESH_MS = $REFRESH_MS;
var WMO = $WMO_OBJ;
function lsGet() {
  try {
    var raw = localStorage.getItem(CACHE_KEY);
    if (!raw) { return null; }
    var o = JSON.parse(raw);
    if (!o || o.fp !== CFG_FP) { return null; }
    return o;
  } catch (e) { return null; }
}
function lsSet(temp, code) {
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify({fp: CFG_FP, t: temp, c: code}));
  } catch (e) {}
}
function paint(temp, code) {
  if (tempEl && temp !== null && temp !== undefined) {
    tempEl.textContent = Math.round(temp) + SYMBOL;
  }
  if (condEl) {
    var w = WMO[code];
    condEl.textContent = w ? ((w.e ? w.e + ' ' : '') + w.l) : '';
  }
}
function fallback() {
  var c = lsGet();
  if (c) { paint(c.t, c.c); return; }
  if (tempEl) { tempEl.textContent = '--' + SYMBOL; }
  if (condEl) { condEl.textContent = 'Weather unavailable'; }
}
function schedule() {
  if (!root || !document.body.contains(root)) { return; }
  setTimeout(refresh, REFRESH_MS + Math.floor(Math.random() * 30000));
}
function refresh() {
  if (!root || !document.body.contains(root)) { return; }
  var ctrl = new AbortController();
  var to = setTimeout(function () { ctrl.abort(); }, 8000);
  fetch(FORECAST_URL, {signal: ctrl.signal})
    .then(function (r) { if (!r.ok) { throw new Error('http ' + r.status); } return r.json(); })
    .then(function (d) {
      var cur = d && d.current ? d.current : null;
      var t = cur ? cur.temperature_2m : null;
      if (t === null || t === undefined) { throw new Error('bad shape'); }
      paint(t, cur.weather_code);
      lsSet(t, cur.weather_code);
    })
    .catch(function () { fallback(); })
    .then(function () { clearTimeout(to); schedule(); });
}
fallback();
refresh();
""".strip()


def _forecast_url(lat: float, lon: float, units: str) -> str:
    unit_param = "fahrenheit" if units == "imperial" else "celsius"
    return (
        "https://api.open-meteo.com/v1/forecast"
        f"?latitude={lat}&longitude={lon}"
        "&current=temperature_2m,weather_code"
        f"&temperature_unit={unit_param}"
    )


def _build_weather_init_js(
    *,
    root_id: str,
    temp_id: str,
    cond_id: str,
    url: str,
    symbol: str,
    cfg_fp: str,
    cache_key: str,
    refresh_ms: int,
) -> str:
    cond_lookup = (
        f"document.getElementById({_js_str(cond_id)})" if cond_id else "null"
    )
    return (
        _WEATHER_INIT_JS_TEMPLATE.replace("$ROOT_ID_LIT", _js_str(root_id))
        .replace("$TEMP_ID_LIT", _js_str(temp_id))
        .replace("$COND_LOOKUP", cond_lookup)
        .replace("$URL_LIT", _js_str(url))
        .replace("$SYMBOL_LIT", _js_str(symbol))
        .replace("$CFG_FP_LIT", _js_str(cfg_fp))
        .replace("$CACHE_KEY_LIT", _js_str(cache_key))
        .replace("$REFRESH_MS", str(refresh_ms))
        .replace("$WMO_OBJ", _wmo_js_object())
    )


class WeatherWidget(Widget):
    """Live current-conditions weather (Open-Meteo, no API key)."""

    slug: ClassVar[str] = "weather"
    display_name: ClassVar[str] = "Weather"
    icon: ClassVar[str] = "\u26c5"
    ConfigSchema: ClassVar[type[BaseModel]] = WeatherWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        return {
            "latitude": _DEFAULT_LAT,
            "longitude": _DEFAULT_LON,
            "location_label": _DEFAULT_LABEL,
            "units": "imperial",
            "show_condition": True,
            "show_location": True,
            "color": "#ffffff",
            "font_family": "sans",
            "font_size_px": 64,
            "refresh_seconds": 900,
            "shrink_to_fit": False,
        }

    def editor_template(self) -> str:
        return "composed/widgets/weather.html"

    def validate_semantic(self, config: BaseModel) -> list[str]:
        assert isinstance(config, WeatherWidgetConfig)
        errors: list[str] = []
        if config.show_location and not config.location_label.strip():
            errors.append(
                "location_label must not be blank when show_location is on"
            )
        return errors

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        del ctx  # weather fetches at runtime; no build-time asset deps
        assert isinstance(config, WeatherWidgetConfig), (
            "WeatherWidget.render_html expects a WeatherWidgetConfig instance"
        )

        css_class = f"cw-weather-{instance_id}"
        root_id = f"cw-weather-root-{instance_id}"
        temp_id = f"cw-weather-temp-{instance_id}"
        cond_id = f"cw-weather-cond-{instance_id}"
        font_stack = _FONT_STACKS[config.font_family]
        symbol = "\u00b0F" if config.units == "imperial" else "\u00b0C"

        if config.shrink_to_fit:
            return self._render_shrink(
                config=config,
                css_class=css_class,
                root_id=root_id,
                temp_id=temp_id,
                cond_id=cond_id,
                font_stack=font_stack,
                symbol=symbol,
                instance_id=instance_id,
            )

        # Location label is the ONLY config-controlled string in the
        # markup — escaped here, never passed into JS.
        loc_html = (
            f'<div class="{css_class}-loc">'
            f"{html.escape(config.location_label)}</div>"
            if config.show_location
            else ""
        )
        cond_html = (
            f'<div id="{cond_id}" class="{css_class}-cond"></div>'
            if config.show_condition
            else ""
        )
        html_out = (
            f'<div id="{root_id}" class="{css_class}">'
            f"{loc_html}"
            f'<div id="{temp_id}" class="{css_class}-temp">--{symbol}</div>'
            f"{cond_html}"
            f"</div>"
        )

        loc_size = max(8, int(config.font_size_px * 0.4))
        cond_size = max(8, int(config.font_size_px * 0.45))
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
            f"  text-align: center;\n"
            f"}}\n"
            f".{css_class}-loc {{\n"
            f"  font-size: {loc_size}px;\n"
            f"  opacity: 0.85;\n"
            f"  margin-bottom: 0.1em;\n"
            f"}}\n"
            f".{css_class}-temp {{\n"
            f"  font-size: {config.font_size_px}px;\n"
            f"  line-height: 1;\n"
            f"  font-variant-numeric: tabular-nums;\n"
            f"}}\n"
            f".{css_class}-cond {{\n"
            f"  font-size: {cond_size}px;\n"
            f"  opacity: 0.9;\n"
            f"  margin-top: 0.1em;\n"
            f"}}"
        )

        cfg_fp = f"{config.latitude},{config.longitude},{config.units}"
        init_js = _build_weather_init_js(
            root_id=root_id,
            temp_id=temp_id,
            cond_id=cond_id if config.show_condition else "",
            url=_forecast_url(config.latitude, config.longitude, config.units),
            symbol=symbol,
            cfg_fp=cfg_fp,
            cache_key=f"cw-weather-{instance_id}",
            refresh_ms=config.refresh_seconds * 1000,
        )

        return WidgetRender(html=html_out, css=css_out, init_js=init_js)

    def _render_shrink(
        self,
        *,
        config: WeatherWidgetConfig,
        css_class: str,
        root_id: str,
        temp_id: str,
        cond_id: str,
        font_stack: str,
        symbol: str,
        instance_id: str,
    ) -> WidgetRender:
        """Shrink-to-fit variant: location + temperature + condition
        auto-scale together to fill the box.

        ``root_id`` stays on the OUTER bounded box (the runtime JS does
        ``document.body.contains(root)`` to stop polling once removed).  The
        three lines are nested in ``#cw-weather-inner-{id}`` which carries the
        base ``px`` size and is what the shared autofit JS fits; child sizes
        are expressed in ``em`` so they scale as a unit.  The per-refresh
        temperature/condition repaint mutates child text nodes, so the shared
        ``MutationObserver`` re-fits automatically.
        """
        inner_id = f"cw-weather-inner-{instance_id}"
        inner_class = f"{css_class}-inner"

        loc_html = (
            f'<div class="{css_class}-loc">'
            f"{html.escape(config.location_label)}</div>"
            if config.show_location
            else ""
        )
        cond_html = (
            f'<div id="{cond_id}" class="{css_class}-cond"></div>'
            if config.show_condition
            else ""
        )
        html_out = (
            f'<div id="{root_id}" class="{css_class}">'
            f'<div id="{inner_id}" class="{inner_class}">'
            f"{loc_html}"
            f'<div id="{temp_id}" class="{css_class}-temp">--{symbol}</div>'
            f"{cond_html}"
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
            f"  text-align: center;\n"
            f"  overflow: hidden;\n"
            f"  box-sizing: border-box;\n"
            f"}}\n"
            f".{inner_class} {{\n"
            f"  display: flex;\n"
            f"  flex-direction: column;\n"
            f"  align-items: center;\n"
            f"  justify-content: center;\n"
            f"  font-size: {config.font_size_px}px;\n"
            f"}}\n"
            f".{css_class}-loc {{\n"
            f"  font-size: 0.4em;\n"
            f"  opacity: 0.85;\n"
            f"  margin-bottom: 0.1em;\n"
            f"}}\n"
            f".{css_class}-temp {{\n"
            f"  font-size: 1em;\n"
            f"  line-height: 1;\n"
            f"  font-variant-numeric: tabular-nums;\n"
            f"}}\n"
            f".{css_class}-cond {{\n"
            f"  font-size: 0.45em;\n"
            f"  opacity: 0.9;\n"
            f"  margin-top: 0.1em;\n"
            f"}}"
        )

        cfg_fp = f"{config.latitude},{config.longitude},{config.units}"
        init_js = (
            _build_weather_init_js(
                root_id=root_id,
                temp_id=temp_id,
                cond_id=cond_id if config.show_condition else "",
                url=_forecast_url(
                    config.latitude, config.longitude, config.units
                ),
                symbol=symbol,
                cfg_fp=cfg_fp,
                cache_key=f"cw-weather-{instance_id}",
                refresh_ms=config.refresh_seconds * 1000,
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
