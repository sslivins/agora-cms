"""Unit tests for the Store Hours composed-slide widget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from cms.composed.registry import get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.store_hours import (
    HolidayOverride,
    Interval,
    StoreHoursWidget,
    StoreHoursWidgetConfig,
)


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


class TestInterval:
    def test_defaults(self):
        iv = Interval()
        assert iv.open == "09:00"
        assert iv.close == "17:00"

    def test_close_must_be_after_open(self):
        with pytest.raises(ValidationError):
            Interval(open="17:00", close="09:00")
        with pytest.raises(ValidationError):
            Interval(open="09:00", close="09:00")

    @pytest.mark.parametrize("bad", ["0900", "09-00", "25:00", "09:60", "24:30"])
    def test_invalid_hhmm_rejected(self, bad):
        with pytest.raises(ValidationError):
            Interval(open=bad)

    def test_midnight_close_allowed(self):
        assert Interval(open="18:00", close="24:00").close == "24:00"

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            Interval(nope=1)


class TestHolidayOverride:
    def test_one_off_and_recurring_dates(self):
        assert HolidayOverride(date="2026-12-25").date == "2026-12-25"
        assert HolidayOverride(date="12-25").date == "12-25"

    @pytest.mark.parametrize("bad", ["2026/12/25", "26-12-25", "13-01", "12-32", "xx-01"])
    def test_invalid_date_rejected(self, bad):
        with pytest.raises(ValidationError):
            HolidayOverride(date=bad)

    def test_closed_default_true(self):
        assert HolidayOverride(date="12-25").closed is True

    def test_open_holiday_keeps_intervals(self):
        h = HolidayOverride(
            date="12-24", closed=False, intervals=[Interval(open="09:00", close="13:00")]
        )
        assert len(h.intervals) == 1

    def test_open_holiday_rejects_overlap(self):
        with pytest.raises(ValidationError):
            HolidayOverride(
                date="12-24",
                closed=False,
                intervals=[
                    Interval(open="09:00", close="13:00"),
                    Interval(open="12:00", close="17:00"),
                ],
            )

    def test_label_length_capped(self):
        with pytest.raises(ValidationError):
            HolidayOverride(date="12-25", label="x" * 81)
        assert HolidayOverride(date="12-25", label="x" * 80).label == "x" * 80


class TestStoreHoursWidgetConfig:
    def test_defaults(self):
        c = StoreHoursWidgetConfig()
        assert c.display_mode == "today"
        assert c.heading == ""
        assert c.show_status is True
        assert c.time_format == "12h"
        assert c.color == "#ffffff"
        assert c.open_color == "#3fb950"
        assert c.closed_color == "#f85149"
        assert c.font_family == "sans"
        assert c.font_size_px == 48
        assert c.holidays == []
        assert c.monday == []

    def test_multi_interval_day(self):
        c = StoreHoursWidgetConfig(
            monday=[
                Interval(open="09:00", close="12:00"),
                Interval(open="13:00", close="17:00"),
            ]
        )
        assert len(c.monday) == 2

    def test_overlapping_intervals_rejected(self):
        with pytest.raises(ValidationError):
            StoreHoursWidgetConfig(
                monday=[
                    Interval(open="09:00", close="13:00"),
                    Interval(open="12:00", close="17:00"),
                ]
            )

    def test_holidays_max_length(self):
        with pytest.raises(ValidationError):
            StoreHoursWidgetConfig(
                holidays=[HolidayOverride(date="12-25")] * 61
            )

    @pytest.mark.parametrize("mode", ["today", "week"])
    def test_valid_display_modes(self, mode):
        assert StoreHoursWidgetConfig(display_mode=mode).display_mode == mode

    def test_invalid_display_mode_rejected(self):
        with pytest.raises(ValidationError):
            StoreHoursWidgetConfig(display_mode="month")

    @pytest.mark.parametrize("fmt", ["12h", "24h"])
    def test_valid_time_formats(self, fmt):
        assert StoreHoursWidgetConfig(time_format=fmt).time_format == fmt

    def test_invalid_time_format_rejected(self):
        with pytest.raises(ValidationError):
            StoreHoursWidgetConfig(time_format="36h")

    def test_heading_length_capped(self):
        with pytest.raises(ValidationError):
            StoreHoursWidgetConfig(heading="x" * 81)
        assert StoreHoursWidgetConfig(heading="x" * 80).heading == "x" * 80

    def test_font_allowlist(self):
        for f in ("sans", "serif", "mono"):
            assert StoreHoursWidgetConfig(font_family=f).font_family == f
        with pytest.raises(ValidationError):
            StoreHoursWidgetConfig(font_family="comic")

    @pytest.mark.parametrize("field", ["color", "open_color", "closed_color"])
    def test_colors_must_be_hex6(self, field):
        StoreHoursWidgetConfig(**{field: "#0aF3Cd"})
        for bad in ("ffffff", "#fff", "#gggggg", "red"):
            with pytest.raises(ValidationError):
                StoreHoursWidgetConfig(**{field: bad})

    def test_font_size_bounds(self):
        StoreHoursWidgetConfig(font_size_px=8)
        StoreHoursWidgetConfig(font_size_px=512)
        for bad in (7, 513, 0, -1):
            with pytest.raises(ValidationError):
                StoreHoursWidgetConfig(font_size_px=bad)

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            StoreHoursWidgetConfig(nope=1)


class TestStoreHoursWidgetRegistry:
    def test_registered(self):
        reg = get_registry()
        assert reg.has("storehours")
        w = reg.get("storehours")
        assert isinstance(w, StoreHoursWidget)
        assert w.display_name == "Store Hours"
        assert w.icon == "🕗"

    def test_default_config_validates(self):
        w = StoreHoursWidget()
        cfg = StoreHoursWidgetConfig(**w.default_config())
        # Mon-Fri 9-5, Sat 10-4, Sun closed.
        assert len(cfg.monday) == 1
        assert cfg.monday[0].open == "09:00"
        assert cfg.saturday[0].close == "16:00"
        assert cfg.sunday == []


class TestStoreHoursWidgetRender:
    def _render(self, **overrides):
        w = StoreHoursWidget()
        cfg = StoreHoursWidgetConfig(**{**w.default_config(), **overrides})
        return w.render_html(cfg, _cell(), "inst-1")

    def test_instance_scoped(self):
        r = self._render()
        assert "cw-storehours-inst-1" in r.html
        assert "cw-storehours-inst-1" in r.css
        assert "cw-storehours-status-inst-1" in r.init_js
        assert "cw-storehours-body-inst-1" in r.init_js

    def test_two_instances_dont_collide(self):
        w = StoreHoursWidget()
        cfg = StoreHoursWidgetConfig(**w.default_config())
        a = w.render_html(cfg, _cell(), "aaa")
        b = w.render_html(cfg, _cell(), "bbb")
        assert "cw-storehours-aaa" in a.css
        assert "cw-storehours-aaa" not in b.css
        assert "cw-storehours-bbb" in b.css

    def test_init_js_has_setinterval(self):
        r = self._render()
        assert "setInterval(render, 60000)" in r.init_js
        assert "getElementById('cw-storehours-status-inst-1')" in r.init_js
        assert "getElementById('cw-storehours-body-inst-1')" in r.init_js

    def test_schedule_baked_as_getday_array(self):
        # Default: Sun closed (index 0 == []), Mon 9-5 == [[540,1020]].
        r = self._render()
        assert "var SCHEDULE = [[],[[540,1020]]" in r.init_js

    def test_holidays_baked_into_js(self):
        r = self._render(
            holidays=[HolidayOverride(date="12-25", label="Xmas", closed=True)]
        )
        assert '"12-25"' in r.init_js
        assert '"Xmas"' in r.init_js
        assert "closed:true" in r.init_js

    def test_open_holiday_intervals_in_js(self):
        r = self._render(
            holidays=[
                HolidayOverride(
                    date="12-24",
                    label="Eve",
                    closed=False,
                    intervals=[Interval(open="09:00", close="13:00")],
                )
            ]
        )
        assert "closed:false" in r.init_js
        assert "intervals:[[540,780]]" in r.init_js

    def test_display_mode_baked(self):
        assert 'var MODE = "today"' in self._render(display_mode="today").init_js
        assert 'var MODE = "week"' in self._render(display_mode="week").init_js

    def test_show_status_flag(self):
        assert "var SHOW_STATUS = true;" in self._render(show_status=True).init_js
        assert "var SHOW_STATUS = false;" in self._render(show_status=False).init_js

    def test_status_element_omitted_when_disabled(self):
        on = self._render(show_status=True)
        off = self._render(show_status=False)
        assert "cw-storehours-status-inst-1" in on.html
        assert 'id="cw-storehours-status-inst-1"' not in off.html

    def test_time_format_flag(self):
        assert "var FMT24 = true;" in self._render(time_format="24h").init_js
        assert "var FMT24 = false;" in self._render(time_format="12h").init_js

    def test_heading_html_escaped(self):
        r = self._render(heading="<b>Hi & Bye</b>")
        assert "<b>Hi & Bye</b>" not in r.html
        assert "&lt;b&gt;Hi &amp; Bye&lt;/b&gt;" in r.html

    def test_no_heading_when_empty(self):
        assert "cw-storehours-inst-1-heading" not in self._render(heading="").html

    def test_colors_in_css_and_js(self):
        r = self._render(
            color="#123456", open_color="#0a0b0c", closed_color="#fefefe"
        )
        assert "color: #123456;" in r.css
        assert '"#0a0b0c"' in r.init_js
        assert '"#fefefe"' in r.init_js

    def test_size_and_font_in_css(self):
        r = self._render(font_size_px=144, font_family="serif")
        assert "font-size: 144px;" in r.css
        assert "Georgia" in r.css
        assert "ui-monospace" in self._render(font_family="mono").css

    def test_no_static_assets(self):
        r = self._render()
        assert r.static_assets == []
        assert r.referenced_asset_ids == []
