"""Tests for cms.composed.widgets.weather.WeatherWidget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import cms.composed.widgets  # noqa: F401
from cms.composed.registry import get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.weather import (
    WeatherWidget,
    WeatherWidgetConfig,
    _DEFAULT_LABEL,
    _DEFAULT_LAT,
    _DEFAULT_LON,
)


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


class TestWeatherWidgetConfig:
    def test_defaults(self):
        c = WeatherWidgetConfig()
        assert c.latitude == _DEFAULT_LAT
        assert c.longitude == _DEFAULT_LON
        assert c.location_label == _DEFAULT_LABEL
        assert c.units == "imperial"
        assert c.show_condition is True
        assert c.show_location is True
        assert c.color == "#ffffff"
        assert c.font_family == "sans"
        assert c.font_size_px == 64
        assert c.refresh_seconds == 900

    def test_default_config_matches_schema(self):
        # A freshly-placed widget must immediately be a valid config.
        w = WeatherWidget()
        WeatherWidgetConfig(**w.default_config())

    def test_units_allowlist(self):
        WeatherWidgetConfig(units="imperial")
        WeatherWidgetConfig(units="metric")
        with pytest.raises(ValidationError):
            WeatherWidgetConfig(units="kelvin")  # type: ignore[arg-type]

    def test_font_allowlist(self):
        for ok in ("sans", "serif", "mono"):
            WeatherWidgetConfig(font_family=ok)
        with pytest.raises(ValidationError):
            WeatherWidgetConfig(font_family="display")

    def test_latitude_bounds(self):
        WeatherWidgetConfig(latitude=-90.0)
        WeatherWidgetConfig(latitude=90.0)
        with pytest.raises(ValidationError):
            WeatherWidgetConfig(latitude=90.1)
        with pytest.raises(ValidationError):
            WeatherWidgetConfig(longitude=-180.1)

    def test_refresh_floor(self):
        WeatherWidgetConfig(refresh_seconds=600)
        with pytest.raises(ValidationError):
            WeatherWidgetConfig(refresh_seconds=599)
        with pytest.raises(ValidationError):
            WeatherWidgetConfig(refresh_seconds=86401)

    def test_label_max_length(self):
        WeatherWidgetConfig(location_label="x" * 80)
        with pytest.raises(ValidationError):
            WeatherWidgetConfig(location_label="x" * 81)

    def test_color_must_be_hex_6(self):
        with pytest.raises(ValidationError):
            WeatherWidgetConfig(color="blue")

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            WeatherWidgetConfig(api_key="secret")  # type: ignore[call-arg]


class TestWeatherWidgetSemantic:
    def test_blank_label_with_show_location_flagged(self):
        w = WeatherWidget()
        errors = w.validate_semantic(
            WeatherWidgetConfig(show_location=True, location_label="   ")
        )
        assert errors and "location_label" in errors[0]

    def test_blank_label_ok_when_location_hidden(self):
        w = WeatherWidget()
        errors = w.validate_semantic(
            WeatherWidgetConfig(show_location=False, location_label="")
        )
        assert errors == []


class TestWeatherWidgetRegistry:
    def test_registered(self):
        w = get_registry().get("weather")
        assert isinstance(w, WeatherWidget)
        assert w.slug == "weather"
        assert w.display_name == "Weather"


class TestWeatherWidgetRender:
    def test_no_declared_assets(self):
        w = WeatherWidget()
        assert w.declared_asset_ids(WeatherWidgetConfig()) == []

    def test_html_and_css_are_instance_scoped(self):
        w = WeatherWidget()
        cfg = WeatherWidgetConfig()
        r1 = w.render_html(cfg, _cell(), "inst-A")
        r2 = w.render_html(cfg, _cell(), "inst-B")
        assert "cw-weather-inst-A" in r1.html
        assert "cw-weather-inst-A" in r1.css
        assert "cw-weather-inst-B" in r2.html
        assert "inst-B" not in r1.html
        assert "inst-A" not in r2.html

    def test_init_js_has_fetch_and_cache_and_abort(self):
        w = WeatherWidget()
        r = w.render_html(WeatherWidgetConfig(), _cell(), "abc")
        assert r.init_js is not None
        js = r.init_js
        assert "fetch(" in js
        assert "localStorage" in js
        assert "AbortController" in js
        assert "open-meteo.com/v1/forecast" in js

    def test_baked_coords_and_units_in_url(self):
        w = WeatherWidget()
        cfg = WeatherWidgetConfig(latitude=40.5, longitude=-74.25, units="metric")
        r = w.render_html(cfg, _cell(), "abc")
        assert r.init_js is not None
        assert "latitude=40.5" in r.init_js
        assert "longitude=-74.25" in r.init_js
        assert "temperature_unit=celsius" in r.init_js

    def test_imperial_url_uses_fahrenheit(self):
        w = WeatherWidget()
        r = w.render_html(WeatherWidgetConfig(units="imperial"), _cell(), "abc")
        assert r.init_js is not None
        assert "temperature_unit=fahrenheit" in r.init_js

    def test_wmo_map_present(self):
        w = WeatherWidget()
        r = w.render_html(WeatherWidgetConfig(), _cell(), "abc")
        assert r.init_js is not None
        # WMO code object literal — clear-sky code 0 should be present.
        assert "var WMO =" in r.init_js
        assert "0:{" in r.init_js

    def test_label_html_escaped_not_in_js(self):
        w = WeatherWidget()
        cfg = WeatherWidgetConfig(location_label="<b>Town</b> & Co")
        r = w.render_html(cfg, _cell(), "abc")
        # Escaped in markup.
        assert "&lt;b&gt;Town&lt;/b&gt; &amp; Co" in r.html
        assert "<b>Town</b>" not in r.html
        # Never interpolated into JS.
        assert r.init_js is not None
        assert "Town" not in r.init_js

    def test_condition_element_toggle(self):
        w = WeatherWidget()
        on = w.render_html(WeatherWidgetConfig(show_condition=True), _cell(), "a")
        off = w.render_html(WeatherWidgetConfig(show_condition=False), _cell(), "b")
        assert "cw-weather-cond-a" in on.html
        assert "cw-weather-cond-b" not in off.html

    def test_location_element_toggle(self):
        w = WeatherWidget()
        on = w.render_html(WeatherWidgetConfig(show_location=True), _cell(), "a")
        off = w.render_html(WeatherWidgetConfig(show_location=False), _cell(), "b")
        assert "cw-weather-a-loc" in on.html
        assert "-loc" not in off.html

    def test_cache_key_fingerprint_changes_with_location(self):
        w = WeatherWidget()
        a = w.render_html(WeatherWidgetConfig(latitude=10.0), _cell(), "abc")
        b = w.render_html(WeatherWidgetConfig(latitude=20.0), _cell(), "abc")
        # Fingerprint embeds lat/lon/units, so the two init scripts differ.
        assert a.init_js != b.init_js

    def test_deterministic_render(self):
        w = WeatherWidget()
        cfg = WeatherWidgetConfig()
        a = w.render_html(cfg, _cell(), "abc")
        b = w.render_html(cfg, _cell(), "abc")
        assert a.html == b.html
        assert a.css == b.css
        assert a.init_js == b.init_js


class TestShrinkToFit:
    def test_default_off_is_byte_identical_to_no_field(self):
        w = WeatherWidget()
        a = w.render_html(WeatherWidgetConfig(font_size_px=64), _cell(), "x")
        b = w.render_html(WeatherWidgetConfig(font_size_px=64, shrink_to_fit=False), _cell(), "x")
        assert a.html == b.html
        assert a.css == b.css
        assert a.js == b.js
        assert a.init_js == b.init_js

    def test_default_off_emits_no_autofit_code(self):
        w = WeatherWidget()
        out = w.render_html(WeatherWidgetConfig(font_size_px=64, shrink_to_fit=False), _cell(), "x")
        assert out.js == ""
        assert "__cwFit" not in out.html
        assert "__cwFit" not in out.css
        assert "__cwFit" not in (out.init_js or "")
        assert "cw-weather-inner-" not in out.html

    def test_on_path_emits_autofit_js_and_init(self):
        w = WeatherWidget()
        out = w.render_html(WeatherWidgetConfig(font_size_px=64, shrink_to_fit=True), _cell(), "abcd")
        assert "window.__cwFit" in out.js
        assert "window.__cwFitObserve" in out.js
        inner_id = "cw-weather-inner-abcd"
        assert inner_id in out.html
        assert inner_id in out.init_js
        assert "__cwFitObserve" in out.init_js
        assert "font-size: 64px" in out.css

    def test_default_config_includes_shrink_to_fit_false(self):
        assert WeatherWidget().default_config()["shrink_to_fit"] is False
