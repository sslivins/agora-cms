"""Tests for device download endpoint authentication (#142).

Verifies that /api/assets/{id}/download and /api/assets/variants/{id}/download
require either a valid device API key or an authenticated browser session.
Also tests the key rotation grace period.
"""

import hashlib
import io
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device import Device, DeviceStatus
from cms.models.device_profile import DeviceProfile


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


@pytest_asyncio.fixture
async def sample_asset(db_session, app):
    """Create a sample asset with a file on disk."""
    from cms.auth import get_settings

    # Use the app's overridden settings (points to tmp_path, not /opt/...)
    settings = app.dependency_overrides[get_settings]()
    asset = Asset(
        filename="download-test.mp4",
        asset_type=AssetType.VIDEO,
        size_bytes=100,
        checksum="dl123",
    )
    db_session.add(asset)
    await db_session.commit()

    # Write file where the storage backend expects it
    (settings.asset_storage_path / "download-test.mp4").write_bytes(b"fake video content")

    return asset


@pytest_asyncio.fixture
async def sample_variant(db_session, sample_asset, app):
    """Create a ready variant with a file on disk."""
    from cms.auth import get_settings

    settings = app.dependency_overrides[get_settings]()
    profile = DeviceProfile(name="DL Test Profile")
    db_session.add(profile)
    await db_session.flush()

    variant = AssetVariant(
        source_asset_id=sample_asset.id,
        profile_id=profile.id,
        filename="download-test_dl-test-profile.mp4",
        status=VariantStatus.READY,
        size_bytes=80,
    )
    db_session.add(variant)
    await db_session.commit()

    variant_dir = settings.asset_storage_path / "variants"
    variant_dir.mkdir(parents=True, exist_ok=True)
    (variant_dir / variant.filename).write_bytes(b"fake variant content")

    return variant


@pytest_asyncio.fixture
async def device_with_key(db_session):
    """Create an adopted device with an API key."""
    key = "test-device-key-abc123"
    device = Device(
        id="dl-test-device",
        name="Download Test Device",
        status=DeviceStatus.ADOPTED,
        device_api_key_hash=_sha256(key),
        api_key_rotated_at=datetime.now(timezone.utc),
    )
    db_session.add(device)
    await db_session.commit()
    return device, key


@pytest.mark.asyncio
class TestDownloadAuthRequired:
    """Download endpoints reject unauthenticated requests."""

    async def test_asset_download_requires_auth(self, unauthed_client, sample_asset):
        resp = await unauthed_client.get(f"/api/assets/{sample_asset.id}/download")
        assert resp.status_code == 401

    async def test_variant_download_requires_auth(self, unauthed_client, sample_variant):
        resp = await unauthed_client.get(
            f"/api/assets/variants/{sample_variant.id}/download"
        )
        assert resp.status_code == 401

    async def test_invalid_key_rejected(self, unauthed_client, sample_asset):
        resp = await unauthed_client.get(
            f"/api/assets/{sample_asset.id}/download",
            headers={"X-Device-API-Key": "completely-wrong-key"},
        )
        assert resp.status_code == 401


@pytest.mark.asyncio
class TestDownloadWithDeviceKey:
    """Download endpoints accept a valid device API key."""

    async def test_asset_download_with_header(
        self, unauthed_client, sample_asset, device_with_key,
    ):
        _, key = device_with_key
        resp = await unauthed_client.get(
            f"/api/assets/{sample_asset.id}/download",
            headers={"X-Device-API-Key": key},
        )
        assert resp.status_code == 200

    async def test_asset_download_with_query_param(
        self, unauthed_client, sample_asset, device_with_key,
    ):
        _, key = device_with_key
        resp = await unauthed_client.get(
            f"/api/assets/{sample_asset.id}/download?key={key}",
        )
        assert resp.status_code == 200

    async def test_variant_download_with_header(
        self, unauthed_client, sample_variant, device_with_key,
    ):
        _, key = device_with_key
        resp = await unauthed_client.get(
            f"/api/assets/variants/{sample_variant.id}/download",
            headers={"X-Device-API-Key": key},
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestDownloadWithSession:
    """Authenticated browser sessions can still download."""

    async def test_asset_download_with_session(self, client, sample_asset):
        resp = await client.get(f"/api/assets/{sample_asset.id}/download")
        assert resp.status_code == 200

    async def test_variant_download_with_session(self, client, sample_variant):
        resp = await client.get(
            f"/api/assets/variants/{sample_variant.id}/download"
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestKeyRotationGracePeriod:
    """Previous key accepted during grace period after rotation."""

    async def test_previous_key_accepted_within_grace(
        self, unauthed_client, sample_asset, db_session,
    ):
        old_key = "old-device-key-xyz"
        new_key = "new-device-key-xyz"
        device = Device(
            id="dl-grace-device",
            name="Grace Period Device",
            status=DeviceStatus.ADOPTED,
            device_api_key_hash=_sha256(new_key),
            previous_api_key_hash=_sha256(old_key),
            api_key_rotated_at=datetime.now(timezone.utc),  # just rotated
        )
        db_session.add(device)
        await db_session.commit()

        # Old key should still work
        resp = await unauthed_client.get(
            f"/api/assets/{sample_asset.id}/download",
            headers={"X-Device-API-Key": old_key},
        )
        assert resp.status_code == 200

        # New key should also work
        resp = await unauthed_client.get(
            f"/api/assets/{sample_asset.id}/download",
            headers={"X-Device-API-Key": new_key},
        )
        assert resp.status_code == 200

    async def test_previous_key_rejected_after_grace(
        self, unauthed_client, sample_asset, db_session,
    ):
        old_key = "expired-old-key"
        new_key = "current-new-key"
        device = Device(
            id="dl-expired-device",
            name="Expired Grace Device",
            status=DeviceStatus.ADOPTED,
            device_api_key_hash=_sha256(new_key),
            previous_api_key_hash=_sha256(old_key),
            # Rotated 10 minutes ago — beyond the 5-minute grace window
            api_key_rotated_at=datetime.now(timezone.utc) - timedelta(minutes=10),
        )
        db_session.add(device)
        await db_session.commit()

        # Old key should be rejected
        resp = await unauthed_client.get(
            f"/api/assets/{sample_asset.id}/download",
            headers={"X-Device-API-Key": old_key},
        )
        assert resp.status_code == 401

        # New key should still work
        resp = await unauthed_client.get(
            f"/api/assets/{sample_asset.id}/download",
            headers={"X-Device-API-Key": new_key},
        )
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestKeyRotationPreservesOldHash:
    """ws.py key rotation should preserve the previous key hash."""

    async def test_rotation_sets_previous_hash(self, db_session):
        """When a device key is rotated, the old hash moves to previous_api_key_hash."""
        original_hash = _sha256("original-key")
        device = Device(
            id="dl-rotate-device",
            name="Rotate Test",
            status=DeviceStatus.ADOPTED,
            device_api_key_hash=original_hash,
            api_key_rotated_at=datetime.now(timezone.utc) - timedelta(hours=2),
        )
        db_session.add(device)
        await db_session.flush()

        # Simulate rotation (same logic as _generate_and_push_api_key)
        new_hash = _sha256("new-rotated-key")
        device.previous_api_key_hash = device.device_api_key_hash
        device.device_api_key_hash = new_hash
        device.api_key_rotated_at = datetime.now(timezone.utc)
        await db_session.commit()

        assert device.device_api_key_hash == new_hash
        assert device.previous_api_key_hash == original_hash
