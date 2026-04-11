"""Tests for the CMS global timezone setting.

Covers the settings page UI, the POST endpoint, build_device_sync timezone
propagation, per-device overrides, and the default UTC fallback.
"""

from datetime import datetime, time, timezone

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.auth import get_setting, set_setting, SETTING_TIMEZONE
from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.setting import CMSSetting
from cms.services.scheduler import build_device_sync


# ── Settings page UI tests ──


@pytest.mark.asyncio
class TestSettingsTimezoneUI:
    """Test the settings page timezone picker."""

    async def test_settings_page_shows_timezone_picker(self, client):
        """Settings page should render the timezone dropdown."""
        resp = await client.get("/settings")
        assert resp.status_code == 200
        text = resp.text
        assert 'name="timezone"' in text
        assert "Save Timezone" in text

    async def test_settings_page_shows_utc_default_warning(self, client):
        """When no timezone is saved, show a warning about UTC default."""
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert "defaulting to UTC" in resp.text

    async def test_settings_page_no_warning_after_save(self, client, db_session):
        """Warning disappears once a timezone has been explicitly saved."""
        await set_setting(db_session, SETTING_TIMEZONE, "America/New_York")
        resp = await client.get("/settings")
        assert resp.status_code == 200
        assert "defaulting to UTC" not in resp.text

    async def test_settings_page_shows_current_timezone(self, client, db_session):
        """Settings page should highlight the currently saved timezone."""
        await set_setting(db_session, SETTING_TIMEZONE, "America/Chicago")
        resp = await client.get("/settings")
        assert resp.status_code == 200
        # The selected option should have 'selected' attribute
        assert "America/Chicago" in resp.text


# ── POST /settings/timezone endpoint tests ──


@pytest.mark.asyncio
class TestChangeTimezone:
    """Test the POST /settings/timezone endpoint."""

    async def test_set_timezone(self, client, db_session):
        """Setting a valid timezone should persist it."""
        resp = await client.post(
            "/settings/timezone",
            data={"timezone": "America/Los_Angeles"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        assert "America/Los_Angeles" in resp.text
        assert "Timezone set to" in resp.text

        # Verify it's persisted in the database
        saved = await get_setting(db_session, SETTING_TIMEZONE)
        assert saved == "America/Los_Angeles"

    async def test_change_timezone(self, client, db_session):
        """Changing from one timezone to another should update."""
        await set_setting(db_session, SETTING_TIMEZONE, "UTC")

        resp = await client.post(
            "/settings/timezone",
            data={"timezone": "Europe/London"},
            follow_redirects=False,
        )
        assert resp.status_code == 200

        saved = await get_setting(db_session, SETTING_TIMEZONE)
        assert saved == "Europe/London"

    async def test_invalid_timezone_rejected(self, client, db_session):
        """An invalid timezone name should return 400."""
        resp = await client.post(
            "/settings/timezone",
            data={"timezone": "Not/A/Timezone"},
            follow_redirects=False,
        )
        assert resp.status_code == 400
        assert "Invalid timezone" in resp.text

    async def test_empty_timezone_rejected(self, client, db_session):
        """An empty timezone should return 400."""
        resp = await client.post(
            "/settings/timezone",
            data={"timezone": ""},
            follow_redirects=False,
        )
        assert resp.status_code == 400

    async def test_timezone_change_does_not_break_page(self, client, db_session):
        """After saving timezone, the page should still render all sections."""
        resp = await client.post(
            "/settings/timezone",
            data={"timezone": "Asia/Tokyo"},
            follow_redirects=False,
        )
        assert resp.status_code == 200
        text = resp.text
        # Page must still contain other settings sections
        assert "System Info" in text
        assert "Change Password" in text
        assert "MCP Server" in text

    async def test_requires_auth(self, unauthed_client):
        """Unauthenticated requests should be rejected."""
        resp = await unauthed_client.post(
            "/settings/timezone",
            data={"timezone": "UTC"},
            follow_redirects=False,
        )
        assert resp.status_code == 401


# ── build_device_sync timezone propagation tests ──


@pytest.mark.asyncio
class TestSyncTimezone:
    """Test that build_device_sync sends the correct timezone to devices."""

    @pytest_asyncio.fixture
    async def db(self, db_engine):
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def _setup_device(self, db, device_id="tz-pi-01", timezone=None, group=None):
        device = Device(
            id=device_id,
            name="TZ Test",
            status=DeviceStatus.ADOPTED,
            timezone=timezone,
        )
        if group:
            device.group = group
        db.add(device)
        await db.commit()
        return device

    async def test_default_utc_when_no_setting(self, db):
        """When no CMS timezone is configured, devices should get UTC."""
        await self._setup_device(db)
        sync = await build_device_sync("tz-pi-01", db)
        assert sync is not None
        assert sync.timezone == "UTC"

    async def test_cms_timezone_sent_to_device(self, db):
        """Device gets the CMS global timezone in sync message."""
        db.add(CMSSetting(key="timezone", value="America/Los_Angeles"))
        await db.commit()
        await self._setup_device(db)

        sync = await build_device_sync("tz-pi-01", db)
        assert sync.timezone == "America/Los_Angeles"

    async def test_device_override_takes_precedence(self, db):
        """Per-device timezone overrides the CMS global timezone."""
        db.add(CMSSetting(key="timezone", value="America/Los_Angeles"))
        await db.commit()
        await self._setup_device(db, timezone="Europe/Berlin")

        sync = await build_device_sync("tz-pi-01", db)
        assert sync.timezone == "Europe/Berlin"

    async def test_device_override_cleared_falls_back_to_cms(self, db):
        """When device timezone is cleared (None), it falls back to CMS global."""
        db.add(CMSSetting(key="timezone", value="Asia/Tokyo"))
        await db.commit()
        await self._setup_device(db, timezone=None)

        sync = await build_device_sync("tz-pi-01", db)
        assert sync.timezone == "Asia/Tokyo"

    async def test_timezone_changes_reflected_in_next_sync(self, db):
        """Changing the CMS timezone should be reflected in the next sync."""
        db.add(CMSSetting(key="timezone", value="UTC"))
        await db.commit()
        await self._setup_device(db)

        sync1 = await build_device_sync("tz-pi-01", db)
        assert sync1.timezone == "UTC"

        # Change timezone
        setting = (await db.execute(
            __import__("sqlalchemy").select(CMSSetting).where(CMSSetting.key == "timezone")
        )).scalar_one()
        setting.value = "America/New_York"
        await db.commit()

        sync2 = await build_device_sync("tz-pi-01", db)
        assert sync2.timezone == "America/New_York"

    async def test_multiple_devices_different_timezones(self, db):
        """Two devices can have different effective timezones."""
        db.add(CMSSetting(key="timezone", value="America/Chicago"))
        await db.commit()

        # Device 1 uses CMS default
        await self._setup_device(db, device_id="tz-pi-01", timezone=None)
        # Device 2 has per-device override
        await self._setup_device(db, device_id="tz-pi-02", timezone="Europe/London")

        sync1 = await build_device_sync("tz-pi-01", db)
        sync2 = await build_device_sync("tz-pi-02", db)

        assert sync1.timezone == "America/Chicago"
        assert sync2.timezone == "Europe/London"

    async def test_sync_includes_timezone_with_schedules(self, db):
        """Timezone is present in sync message alongside schedules."""
        db.add(CMSSetting(key="timezone", value="America/Denver"))
        await db.commit()

        asset = Asset(
            filename="tz-video.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=1000,
            checksum="abc",
        )
        db.add(asset)
        await db.flush()

        await self._setup_device(db)

        sched = Schedule(
            name="TZ Schedule",
            device_id="tz-pi-01",
            asset_id=asset.id,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        db.add(sched)
        await db.commit()

        sync = await build_device_sync("tz-pi-01", db)
        assert sync.timezone == "America/Denver"
        assert len(sync.schedules) == 1


# ── Server time API tests ──


@pytest.mark.asyncio
class TestServerTimeAPI:
    """Test the /api/server-time endpoint reflects the configured timezone."""

    async def test_server_time_default_utc(self, client):
        """When no timezone is set, /api/server-time should return UTC."""
        resp = await client.get("/api/server-time")
        assert resp.status_code == 200
        data = resp.json()
        assert data["timezone"] == "UTC"

    async def test_server_time_reflects_setting(self, client, db_session):
        """After setting timezone, /api/server-time should return it."""
        await set_setting(db_session, SETTING_TIMEZONE, "America/Los_Angeles")
        resp = await client.get("/api/server-time")
        assert resp.status_code == 200
        data = resp.json()
        assert data["timezone"] == "America/Los_Angeles"
        assert "local" in data
        assert "utc" in data
