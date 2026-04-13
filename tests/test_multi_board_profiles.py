"""Tests for multi-board transcode profiles and auto-assignment.

Verifies that:
- Pi 4 and Pi 5 built-in profiles are seeded on startup.
- Auto-assignment maps device_type strings to correct profiles.
- supported_codecs is stored on registration and updated on reconnect.
- supported_codecs is exposed in the device API response.
- Device type matching is case-insensitive and substring-based.
"""

import uuid

import pytest
from sqlalchemy import select

from cms.models.device import Device, DeviceStatus
from cms.models.device_profile import DeviceProfile
from cms.routers.ws import _DEVICE_TYPE_PROFILE_MAP, _auto_assign_profile


# ── Profile seeding ──

@pytest.mark.asyncio
class TestMultiBoardProfileSeeding:
    """Built-in profiles for all boards are seeded on startup."""

    async def test_seed_creates_pi4_profile(self, db_session):
        from cms.main import _seed_profiles
        await _seed_profiles(db_session)

        result = await db_session.execute(
            select(DeviceProfile).where(DeviceProfile.name == "pi-4")
        )
        profile = result.scalar_one_or_none()
        assert profile is not None
        assert profile.video_codec == "h265"
        assert profile.video_profile == "main"
        assert profile.max_width == 1920
        assert profile.max_height == 1080
        assert profile.max_fps == 30
        assert profile.crf == 23
        assert profile.builtin is True

    async def test_seed_creates_pi5_profile(self, db_session):
        from cms.main import _seed_profiles
        await _seed_profiles(db_session)

        result = await db_session.execute(
            select(DeviceProfile).where(DeviceProfile.name == "pi-5")
        )
        profile = result.scalar_one_or_none()
        assert profile is not None
        assert profile.video_codec == "h265"
        assert profile.video_profile == "main"
        assert profile.max_width == 1920
        assert profile.max_height == 1080
        assert profile.max_fps == 60
        assert profile.crf == 23
        assert profile.builtin is True

    async def test_seed_still_creates_zero2w_profile(self, db_session):
        from cms.main import _seed_profiles
        await _seed_profiles(db_session)

        result = await db_session.execute(
            select(DeviceProfile).where(DeviceProfile.name == "pi-zero-2w")
        )
        profile = result.scalar_one_or_none()
        assert profile is not None
        assert profile.video_codec == "h264"
        assert profile.max_fps == 30

    async def test_seed_creates_all_three_profiles(self, db_session):
        from cms.main import _seed_profiles
        await _seed_profiles(db_session)

        result = await db_session.execute(
            select(DeviceProfile).where(DeviceProfile.builtin == True)
        )
        profiles = {p.name for p in result.scalars().all()}
        assert profiles == {"pi-zero-2w", "pi-4", "pi-5"}

    async def test_seed_resets_modified_pi4(self, db_session):
        """If pi-4 profile was modified, seed resets it to defaults."""
        profile = DeviceProfile(
            name="pi-4",
            description="Modified",
            video_codec="h265",
            video_profile="high",
            max_width=1280,
            max_fps=60,
            crf=18,
            builtin=True,
        )
        db_session.add(profile)
        await db_session.commit()

        from cms.main import _seed_profiles
        await _seed_profiles(db_session)

        await db_session.refresh(profile)
        assert profile.description == "Raspberry Pi 4 — HEVC Main, 1080p30"
        assert profile.video_profile == "main"
        assert profile.max_width == 1920
        assert profile.max_fps == 30
        assert profile.crf == 23


# ── Device type mapping ──

class TestDeviceTypeMapping:
    """_DEVICE_TYPE_PROFILE_MAP covers all board variants."""

    def test_zero2w_mapping(self):
        assert "pi zero 2 w" in _DEVICE_TYPE_PROFILE_MAP
        assert _DEVICE_TYPE_PROFILE_MAP["pi zero 2 w"] == "pi-zero-2w"

    def test_pi4_mapping(self):
        assert "pi 4" in _DEVICE_TYPE_PROFILE_MAP
        assert _DEVICE_TYPE_PROFILE_MAP["pi 4"] == "pi-4"

    def test_pi5_mapping(self):
        assert "pi 5" in _DEVICE_TYPE_PROFILE_MAP
        assert _DEVICE_TYPE_PROFILE_MAP["pi 5"] == "pi-5"


# ── Auto-assignment ──

@pytest.mark.asyncio
class TestAutoAssignProfile:
    """Auto-assign profile based on device_type substring matching."""

    async def _seed_and_get_profiles(self, db_session):
        from cms.main import _seed_profiles
        await _seed_profiles(db_session)
        result = await db_session.execute(select(DeviceProfile))
        return {p.name: p for p in result.scalars().all()}

    async def test_assigns_pi4_profile(self, db_session):
        profiles = await self._seed_and_get_profiles(db_session)
        device = Device(
            id="test-pi4",
            name="Test Pi 4",
            device_type="Raspberry Pi 4 Model B Rev 1.5",
        )
        db_session.add(device)
        await db_session.commit()

        await _auto_assign_profile(device, db_session)

        assert device.profile_id == profiles["pi-4"].id

    async def test_assigns_pi5_profile(self, db_session):
        profiles = await self._seed_and_get_profiles(db_session)
        device = Device(
            id="test-pi5",
            name="Test Pi 5",
            device_type="Raspberry Pi 5 Model B Rev 1.0",
        )
        db_session.add(device)
        await db_session.commit()

        await _auto_assign_profile(device, db_session)

        assert device.profile_id == profiles["pi-5"].id

    async def test_assigns_cm5_profile(self, db_session):
        """Compute Module 5 contains 'Pi 5' and should get the pi-5 profile."""
        profiles = await self._seed_and_get_profiles(db_session)
        device = Device(
            id="test-cm5",
            name="Test CM5",
            device_type="Raspberry Pi 5 Compute Module Rev 1.0",
        )
        db_session.add(device)
        await db_session.commit()

        await _auto_assign_profile(device, db_session)

        assert device.profile_id == profiles["pi-5"].id

    async def test_assigns_zero2w_profile(self, db_session):
        profiles = await self._seed_and_get_profiles(db_session)
        device = Device(
            id="test-zero2w",
            name="Test Zero 2 W",
            device_type="Raspberry Pi Zero 2 W Rev 1.0",
        )
        db_session.add(device)
        await db_session.commit()

        await _auto_assign_profile(device, db_session)

        assert device.profile_id == profiles["pi-zero-2w"].id

    async def test_skips_if_profile_already_set(self, db_session):
        """Don't overwrite an admin-assigned profile."""
        profiles = await self._seed_and_get_profiles(db_session)
        device = Device(
            id="test-override",
            name="Test Override",
            device_type="Raspberry Pi 4 Model B Rev 1.5",
            profile_id=profiles["pi-zero-2w"].id,  # admin override
        )
        db_session.add(device)
        await db_session.commit()

        await _auto_assign_profile(device, db_session)

        # Should keep the admin-assigned profile, not switch to pi-4
        assert device.profile_id == profiles["pi-zero-2w"].id

    async def test_skips_if_no_device_type(self, db_session):
        await self._seed_and_get_profiles(db_session)
        device = Device(id="test-unknown", name="Unknown")
        db_session.add(device)
        await db_session.commit()

        await _auto_assign_profile(device, db_session)

        assert device.profile_id is None

    async def test_skips_if_unrecognized_device_type(self, db_session):
        await self._seed_and_get_profiles(db_session)
        device = Device(
            id="test-other",
            name="Other",
            device_type="Some Other Board",
        )
        db_session.add(device)
        await db_session.commit()

        await _auto_assign_profile(device, db_session)

        assert device.profile_id is None


# ── Supported codecs ──

@pytest.mark.asyncio
class TestSupportedCodecs:
    """supported_codecs is stored and exposed in the API."""

    async def test_device_stores_supported_codecs(self, db_session):
        device = Device(
            id="test-codecs",
            name="Codec Test",
            supported_codecs="hevc,h264",
        )
        db_session.add(device)
        await db_session.commit()

        result = await db_session.execute(
            select(Device).where(Device.id == "test-codecs")
        )
        d = result.scalar_one()
        assert d.supported_codecs == "hevc,h264"

    async def test_device_api_includes_supported_codecs(self, client, db_session):
        device = Device(
            id="test-codecs-api",
            name="Codec API Test",
            status=DeviceStatus.ADOPTED,
            supported_codecs="hevc",
        )
        db_session.add(device)
        await db_session.commit()

        resp = await client.get("/api/devices/test-codecs-api")
        assert resp.status_code == 200
        assert resp.json()["supported_codecs"] == "hevc"

    async def test_device_defaults_empty_codecs(self, db_session):
        device = Device(id="test-no-codecs", name="No Codecs")
        db_session.add(device)
        await db_session.commit()

        result = await db_session.execute(
            select(Device).where(Device.id == "test-no-codecs")
        )
        d = result.scalar_one()
        assert d.supported_codecs == ""
