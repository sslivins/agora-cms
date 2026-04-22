"""Unit tests for cms.services.device_register.register_known_device.

Exercises the shared helper that both the direct-WebSocket endpoint
(cms/routers/ws.py) and the WPS upstream webhook (cms/routers/wps_webhook.py)
use to process a ``register`` message from a device whose row already
exists.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cms.models.device import DeviceStatus
from cms.services.device_register import (
    hash_token,
    register_known_device,
)


class _FakeDB:
    """Tiny async-session stand-in that records commits + can return
    a DeviceProfile row for the auto-assign query."""

    def __init__(self, profile_row=None):
        self.commits = 0
        self.profile_row = profile_row

    async def execute(self, stmt, *args, **kwargs):
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=self.profile_row)
        return result

    async def commit(self):
        self.commits += 1


def _make_device(
    *,
    id: str = "pi-1",
    name: str = "Kiosk",
    device_auth_token_hash: str | None = None,
    status: DeviceStatus = DeviceStatus.ADOPTED,
    device_type: str = "",
    firmware_version: str = "1.0.0",
    supported_codecs: str = "",
    storage_capacity_mb: int = 0,
    storage_used_mb: int = 0,
    profile_id=None,
) -> MagicMock:
    """Build a MagicMock that quacks like a Device ORM row with the
    writable attributes the helper touches."""
    d = MagicMock()
    d.id = id
    d.name = name
    d.device_auth_token_hash = device_auth_token_hash
    d.status = status
    d.device_type = device_type
    d.firmware_version = firmware_version
    d.supported_codecs = supported_codecs
    d.storage_capacity_mb = storage_capacity_mb
    d.storage_used_mb = storage_used_mb
    d.profile_id = profile_id
    d.last_seen = None
    return d


@pytest.mark.asyncio
class TestRegisterKnownDevice:
    async def test_valid_auth_token_accepts_without_new_token(self):
        token = "known-token"
        device = _make_device(device_auth_token_hash=hash_token(token))
        db = _FakeDB()

        result = await register_known_device(
            device,
            {
                "type": "register",
                "device_id": "pi-1",
                "auth_token": token,
                "firmware_version": "1.11.7",
            },
            db,
        )

        assert result.orphaned is False
        assert result.auth_assigned is None
        assert device.firmware_version == "1.11.7"
        assert device.status == DeviceStatus.ADOPTED
        # device_auth_token_hash must be unchanged
        assert device.device_auth_token_hash == hash_token(token)

    async def test_empty_auth_token_treats_as_reflashed(self):
        """Empty token + existing hash → reset to PENDING, mint new token."""
        device = _make_device(
            device_auth_token_hash=hash_token("old-token"),
            status=DeviceStatus.ADOPTED,
        )
        db = _FakeDB()

        result = await register_known_device(
            device,
            {
                "type": "register",
                "device_id": "pi-1",
                "auth_token": "",
            },
            db,
        )

        assert result.orphaned is False
        assert result.auth_assigned is not None
        assert result.auth_assigned["type"] == "auth_assigned"
        assert "device_auth_token" in result.auth_assigned
        assert device.status == DeviceStatus.PENDING
        # Hash should have been replaced with a new one.
        assert device.device_auth_token_hash is not None
        assert device.device_auth_token_hash != hash_token("old-token")
        assert device.device_auth_token_hash == hash_token(
            result.auth_assigned["device_auth_token"],
        )

    async def test_wrong_auth_token_marks_orphaned(self):
        device = _make_device(device_auth_token_hash=hash_token("real-token"))
        db = _FakeDB()

        result = await register_known_device(
            device,
            {
                "type": "register",
                "device_id": "pi-1",
                "auth_token": "impostor-token",
            },
            db,
        )

        assert result.orphaned is True
        assert result.auth_assigned is None
        assert device.status == DeviceStatus.ORPHANED

    async def test_no_stored_token_mints_one(self):
        """Device exists but has never been assigned an auth token."""
        device = _make_device(device_auth_token_hash=None)
        db = _FakeDB()

        result = await register_known_device(
            device,
            {"type": "register", "device_id": "pi-1", "auth_token": ""},
            db,
        )

        assert result.orphaned is False
        assert result.auth_assigned is not None
        assert device.device_auth_token_hash is not None
        assert device.device_auth_token_hash == hash_token(
            result.auth_assigned["device_auth_token"],
        )

    async def test_metadata_refresh_from_payload(self):
        device = _make_device(
            device_auth_token_hash=hash_token("t"),
            firmware_version="1.0.0",
            device_type="",
            supported_codecs="",
            storage_capacity_mb=0,
            storage_used_mb=0,
        )
        db = _FakeDB()

        await register_known_device(
            device,
            {
                "type": "register",
                "device_id": "pi-1",
                "auth_token": "t",
                "firmware_version": "1.12.0",
                "device_type": "Raspberry Pi 5",
                "supported_codecs": ["h264", "vp8"],
                "storage_capacity_mb": 32768,
                "storage_used_mb": 100,
            },
            db,
        )

        assert device.firmware_version == "1.12.0"
        assert device.device_type == "Raspberry Pi 5"
        assert device.supported_codecs == "h264,vp8"
        assert device.storage_capacity_mb == 32768
        assert device.storage_used_mb == 100
        assert device.last_seen is not None

    async def test_device_name_not_overridden_unless_custom(self):
        """Register payloads only update device.name when the captive-portal
        flag device_name_custom=True is set — otherwise keep the CMS-side
        name."""
        device = _make_device(
            device_auth_token_hash=hash_token("t"), name="Lobby-CMS-Named",
        )
        db = _FakeDB()

        # Default: no custom flag → name unchanged
        await register_known_device(
            device,
            {
                "type": "register", "device_id": "pi-1", "auth_token": "t",
                "device_name": "something-from-device",
            },
            db,
        )
        assert device.name == "Lobby-CMS-Named"

        # With the custom flag → name is replaced
        await register_known_device(
            device,
            {
                "type": "register", "device_id": "pi-1", "auth_token": "t",
                "device_name": "User-Picked-Name",
                "device_name_custom": True,
            },
            db,
        )
        assert device.name == "User-Picked-Name"

    async def test_auto_assign_profile_runs_when_unset(self):
        """When device has no profile and device_type matches the map,
        the helper looks up + assigns the matching profile."""
        device = _make_device(
            device_auth_token_hash=hash_token("t"),
            device_type="Raspberry Pi 5",
            profile_id=None,
        )
        profile_row = MagicMock(id="profile-pi5-uuid", name="pi-5")
        db = _FakeDB(profile_row=profile_row)

        await register_known_device(
            device,
            {"type": "register", "device_id": "pi-1", "auth_token": "t"},
            db,
        )

        assert device.profile_id == "profile-pi5-uuid"

    async def test_auto_assign_profile_skipped_when_already_set(self):
        device = _make_device(
            device_auth_token_hash=hash_token("t"),
            device_type="Raspberry Pi 5",
            profile_id="already-set",
        )
        db = _FakeDB(profile_row=MagicMock(id="other", name="pi-5"))

        await register_known_device(
            device,
            {"type": "register", "device_id": "pi-1", "auth_token": "t"},
            db,
        )

        assert device.profile_id == "already-set"
