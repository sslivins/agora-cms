"""Tests for the curated timezone list (#206).

Ensures the CMS only offers a compact, device-compatible set of major IANA
timezones covering every practical UTC offset.
"""

import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from cms.timezones import (
    CURATED_TIMEZONES,
    build_tz_options,
    canonical_timezones,
)


# ── Unit tests for cms.timezones ──


class TestCuratedTimezones:
    """Verify the curated timezone set has the right properties."""

    def test_returns_frozenset(self):
        result = canonical_timezones()
        assert isinstance(result, frozenset)

    def test_is_curated_set(self):
        """canonical_timezones() should return the CURATED_TIMEZONES constant."""
        assert canonical_timezones() is CURATED_TIMEZONES

    def test_utc_included(self):
        assert "UTC" in canonical_timezones()

    def test_major_us_zones_included(self):
        zones = canonical_timezones()
        for tz in [
            "America/New_York",
            "America/Chicago",
            "America/Denver",
            "America/Los_Angeles",
            "America/Phoenix",
            "America/Anchorage",
            "Pacific/Honolulu",
        ]:
            assert tz in zones, f"Expected {tz} in curated set"

    def test_major_world_zones_included(self):
        zones = canonical_timezones()
        for tz in [
            "Europe/London",
            "Europe/Paris",
            "Europe/Berlin",
            "Asia/Tokyo",
            "Asia/Shanghai",
            "Asia/Kolkata",
            "Australia/Sydney",
            "Pacific/Auckland",
        ]:
            assert tz in zones, f"Expected {tz} in curated set"

    def test_half_hour_offsets_included(self):
        """Should include fractional-offset zones."""
        zones = canonical_timezones()
        assert "Asia/Kolkata" in zones       # +05:30
        assert "America/St_Johns" in zones   # -03:30
        assert "Asia/Tehran" in zones        # +03:30
        assert "Asia/Kathmandu" in zones     # +05:45

    def test_deprecated_aliases_excluded(self):
        zones = canonical_timezones()
        for tz in ["US/Eastern", "US/Pacific", "US/Central",
                    "Canada/Eastern", "Canada/Pacific",
                    "Brazil/East", "Chile/Continental"]:
            assert tz not in zones, f"Deprecated alias {tz} should be excluded"

    def test_etc_offsets_excluded(self):
        zones = canonical_timezones()
        for tz in ["Etc/GMT+5", "Etc/GMT-7", "Etc/GMT0", "Etc/UTC"]:
            assert tz not in zones, f"Etc/ zone {tz} should be excluded"

    def test_posix_abbreviations_excluded(self):
        zones = canonical_timezones()
        for tz in ["EST", "CST6CDT", "MST7MDT", "GMT", "CET", "EET"]:
            assert tz not in zones, f"POSIX abbreviation {tz} should be excluded"

    def test_all_entries_match_region_city_or_utc(self):
        """Every entry should be Region/City or UTC."""
        pattern = re.compile(r"^[A-Z][a-z]+/[A-Z]")
        for tz in canonical_timezones():
            if tz == "UTC":
                continue
            assert pattern.match(tz), f"{tz} doesn't match Region/City pattern"

    def test_compact_count(self):
        """Should have a compact set — between 30 and 50 zones."""
        count = len(canonical_timezones())
        assert 30 <= count <= 50, f"Expected 30-50 curated zones, got {count}"

    def test_covers_all_major_offsets(self):
        """Should cover a wide range of UTC offsets from -11 to +13."""
        from datetime import datetime, timezone
        from zoneinfo import ZoneInfo

        now = datetime.now(timezone.utc)
        offsets_hours = set()
        for tz_name in canonical_timezones():
            if tz_name == "UTC":
                offsets_hours.add(0)
                continue
            try:
                offset = now.astimezone(ZoneInfo(tz_name)).utcoffset()
                offsets_hours.add(int(offset.total_seconds()) // 3600)
            except Exception:
                pass
        # Should span from deep negative to deep positive
        assert min(offsets_hours) <= -8, f"Westernmost offset {min(offsets_hours)} not far enough"
        assert max(offsets_hours) >= 12, f"Easternmost offset {max(offsets_hours)} not far enough"
        assert len(offsets_hours) >= 20, f"Only {len(offsets_hours)} distinct offsets — too few"


class TestBuildTzOptions:
    """Verify the picker option builder produces correct output."""

    def test_returns_list_of_dicts(self):
        options = build_tz_options()
        assert isinstance(options, list)
        assert len(options) == len(CURATED_TIMEZONES)
        for opt in options:
            assert "value" in opt
            assert "label" in opt

    def test_values_are_curated(self):
        for opt in build_tz_options():
            assert opt["value"] in CURATED_TIMEZONES

    def test_sorted_by_offset(self):
        """Options should be sorted by UTC offset (west to east)."""
        options = build_tz_options()
        # Extract offset from labels
        offsets = []
        for opt in options:
            match = re.search(r"UTC([+-])(\d{2}):(\d{2})", opt["label"])
            assert match, f"No offset in label: {opt['label']}"
            sign = 1 if match.group(1) == "+" else -1
            minutes = int(match.group(2)) * 60 + int(match.group(3))
            offsets.append(sign * minutes)
        assert offsets == sorted(offsets), "Options should be sorted by UTC offset"

    def test_labels_contain_utc_offset(self):
        for opt in build_tz_options():
            assert re.search(r"UTC[+-]\d{2}:\d{2}", opt["label"]), \
                f"Label missing offset format: {opt['label']}"

    def test_underscores_replaced_in_labels(self):
        options = build_tz_options()
        ny = next((o for o in options if o["value"] == "America/New_York"), None)
        assert ny is not None
        assert "New York" in ny["label"]
        assert "New_York" not in ny["label"]

    def test_deprecated_not_in_options(self):
        values = {opt["value"] for opt in build_tz_options()}
        assert "US/Eastern" not in values
        assert "Etc/GMT+5" not in values


# ── Integration tests for the UI endpoints ──


@pytest.mark.asyncio
class TestTimezoneEndpoints:
    """Verify the settings and schedule pages use the curated set."""

    async def test_settings_page_uses_curated_timezones(self, client):
        """The settings page timezone dropdown should only contain curated zones."""
        resp = await client.get("/settings")
        assert resp.status_code == 200
        text = resp.text

        # Should contain curated zones
        assert "America/New_York" in text
        assert "Europe/London" in text

        # Should NOT contain deprecated aliases
        assert "US/Eastern" not in text
        assert "Canada/Pacific" not in text
        assert "Etc/GMT+5" not in text

        # Should NOT contain non-curated canonical zones
        assert "America/Indiana" not in text
        assert "Africa/Abidjan" not in text

    async def test_schedules_page_uses_curated_timezones(self, client):
        """The schedules page timezone dropdown should only contain curated zones."""
        resp = await client.get("/schedules")
        assert resp.status_code == 200
        text = resp.text

        assert "America/Chicago" in text
        assert "US/Central" not in text

    async def test_save_curated_timezone_succeeds(self, client):
        """Saving a curated timezone should succeed."""
        resp = await client.post(
            "/settings/timezone",
            data={"timezone": "America/Denver"},
        )
        assert resp.status_code == 200
        assert "America/Denver" in resp.text

    async def test_save_deprecated_timezone_rejected(self, client):
        """Saving a deprecated timezone alias should be rejected."""
        resp = await client.post(
            "/settings/timezone",
            data={"timezone": "US/Mountain"},
        )
        assert resp.status_code == 400
        assert "Invalid timezone" in resp.text

    async def test_save_non_curated_canonical_rejected(self, client):
        """Saving a valid IANA zone not in the curated list should be rejected."""
        resp = await client.post(
            "/settings/timezone",
            data={"timezone": "America/Indiana/Indianapolis"},
        )
        assert resp.status_code == 400

    async def test_save_etc_timezone_rejected(self, client):
        resp = await client.post(
            "/settings/timezone",
            data={"timezone": "Etc/GMT+7"},
        )
        assert resp.status_code == 400

    async def test_save_posix_abbreviation_rejected(self, client):
        resp = await client.post(
            "/settings/timezone",
            data={"timezone": "EST"},
        )
        assert resp.status_code == 400

    async def test_save_utc_succeeds(self, client):
        resp = await client.post(
            "/settings/timezone",
            data={"timezone": "UTC"},
        )
        assert resp.status_code == 200

    async def test_save_nonsense_rejected(self, client):
        resp = await client.post(
            "/settings/timezone",
            data={"timezone": "Not/A/Real/Zone"},
        )
        assert resp.status_code == 400

    async def test_device_page_loads_without_deprecated(self, client):
        resp = await client.get("/devices")
        assert resp.status_code == 200
        text = resp.text
        assert "US/Eastern" not in text
        assert "Canada/Pacific" not in text

    def test_common_timezones_list_is_curated(self):
        """COMMON_TIMEZONES module-level list should match curated set."""
        from cms.ui import COMMON_TIMEZONES

        assert set(COMMON_TIMEZONES) == CURATED_TIMEZONES
        assert COMMON_TIMEZONES == sorted(COMMON_TIMEZONES)
