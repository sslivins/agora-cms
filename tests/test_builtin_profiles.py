"""Tests for built-in profile protection, editing, reset, and copy.

Verifies that:
- Built-in profiles CAN be edited (PUT returns 200).
- Built-in profiles cannot be deleted (existing behavior).
- Non-built-in profiles can still be edited and deleted.
- POST /api/profiles/{id}/copy creates a duplicate with a unique name.
- POST /api/profiles/{id}/reset restores canonical defaults.
- Copying a non-existent profile returns 404.
- Copying the same profile twice produces distinct names.
- The seed function creates missing profiles but preserves customizations.
"""

import uuid

import pytest
from sqlalchemy import select

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device_profile import DeviceProfile


@pytest.mark.asyncio
class TestBuiltinProfileEditable:
    """Built-in profiles can be edited but not deleted."""

    async def test_edit_builtin_allowed(self, client, db_session):
        """PUT on a built-in profile should succeed."""
        profile = DeviceProfile(
            name="test-builtin", video_codec="h264",
            video_profile="main", builtin=True,
        )
        db_session.add(profile)
        await db_session.commit()

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"description": "modified"},
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "modified"

    async def test_delete_builtin_returns_400(self, client, db_session):
        """DELETE on a built-in profile should return 400."""
        profile = DeviceProfile(
            name="test-builtin-del", video_codec="h264",
            video_profile="main", builtin=True,
        )
        db_session.add(profile)
        await db_session.commit()

        resp = await client.delete(f"/api/profiles/{profile.id}")
        assert resp.status_code == 400
        assert "built-in" in resp.json()["detail"].lower()

    async def test_edit_non_builtin_allowed(self, client, db_session):
        """PUT on a non-built-in profile should succeed."""
        profile = DeviceProfile(
            name="test-custom", video_codec="h264",
            video_profile="main", builtin=False,
        )
        db_session.add(profile)
        await db_session.commit()

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"description": "updated desc"},
        )
        assert resp.status_code == 200
        assert resp.json()["description"] == "updated desc"


@pytest.mark.asyncio
class TestCopyProfile:
    """POST /api/profiles/{id}/copy creates a duplicate profile."""

    async def test_copy_creates_new_profile(self, client, db_session):
        """Copying a profile creates a new one with 'Copy of' prefix."""
        profile = DeviceProfile(
            name="original", video_codec="h264",
            video_profile="main", max_width=1280, max_height=720,
            crf=18, builtin=True,
        )
        db_session.add(profile)
        await db_session.commit()

        resp = await client.post(f"/api/profiles/{profile.id}/copy")
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Copy of original"
        assert data["video_codec"] == "h264"
        assert data["max_width"] == 1280
        assert data["max_height"] == 720
        assert data["crf"] == 18
        assert data["builtin"] is False

    async def test_copy_nonexistent_returns_404(self, client):
        """Copying a non-existent profile should return 404."""
        fake_id = uuid.uuid4()
        resp = await client.post(f"/api/profiles/{fake_id}/copy")
        assert resp.status_code == 404

    async def test_copy_twice_gets_unique_names(self, client, db_session):
        """Copying the same profile twice produces distinct names."""
        profile = DeviceProfile(
            name="base-profile", video_codec="h264",
            video_profile="main",
        )
        db_session.add(profile)
        await db_session.commit()

        resp1 = await client.post(f"/api/profiles/{profile.id}/copy")
        assert resp1.status_code == 201
        assert resp1.json()["name"] == "Copy of base-profile"

        resp2 = await client.post(f"/api/profiles/{profile.id}/copy")
        assert resp2.status_code == 201
        assert resp2.json()["name"] == "Copy of base-profile 2"

    async def test_copy_non_builtin_profile(self, client, db_session):
        """Copying a non-built-in profile should also work."""
        profile = DeviceProfile(
            name="user-profile", video_codec="h265",
            video_profile="main10", pixel_format="yuv420p10le",
            color_space="bt2020-pq",
        )
        db_session.add(profile)
        await db_session.commit()

        resp = await client.post(f"/api/profiles/{profile.id}/copy")
        assert resp.status_code == 201
        data = resp.json()
        assert data["video_codec"] == "h265"
        assert data["pixel_format"] == "yuv420p10le"
        assert data["color_space"] == "bt2020-pq"
        assert data["builtin"] is False

    async def test_copy_enqueues_variants(self, client, db_session):
        """Copying a profile should enqueue variants for existing video assets."""
        # Create a video asset first
        asset = Asset(
            filename="copy-test.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        profile = DeviceProfile(
            name="to-copy", video_codec="h264", video_profile="main",
        )
        db_session.add(profile)
        await db_session.commit()

        resp = await client.post(f"/api/profiles/{profile.id}/copy")
        assert resp.status_code == 201
        data = resp.json()
        assert data["total_variants"] >= 1


@pytest.mark.asyncio
class TestSeedPreservesCustomizations:
    """_seed_profiles creates missing profiles but preserves customizations."""

    async def test_seed_preserves_modified_builtin(self, db_session):
        """If a built-in profile was modified, seed should NOT restore defaults."""
        profile = DeviceProfile(
            name="pi-zero-2w",
            description="Modified",
            video_codec="h264",
            video_profile="high",  # default is "main"
            max_width=1280,        # default is 1920
            max_height=720,        # default is 1080
            max_fps=60,            # default is 30
            crf=18,                # default is 23
            audio_codec="mp3",     # default is "aac"
            audio_bitrate="64k",   # default is "128k"
            builtin=True,
        )
        db_session.add(profile)
        await db_session.commit()

        from cms.main import _seed_profiles
        await _seed_profiles(db_session)

        await db_session.refresh(profile)
        # Customizations should be preserved
        assert profile.description == "Modified"
        assert profile.video_profile == "high"
        assert profile.max_width == 1280
        assert profile.max_height == 720
        assert profile.max_fps == 60
        assert profile.crf == 18
        assert profile.audio_codec == "mp3"
        assert profile.audio_bitrate == "64k"
        assert profile.builtin is True


@pytest.mark.asyncio
class TestResetProfile:
    """POST /api/profiles/{id}/reset restores canonical defaults."""

    async def test_reset_builtin_restores_defaults(self, client, db_session):
        """Resetting a modified builtin should restore all default values."""
        profile = DeviceProfile(
            name="pi-zero-2w",
            description="Custom desc",
            video_codec="h264",
            video_profile="high",
            max_width=1280,
            max_height=720,
            max_fps=60,
            crf=18,
            audio_codec="mp3",
            audio_bitrate="64k",
            builtin=True,
        )
        db_session.add(profile)
        await db_session.commit()

        resp = await client.post(f"/api/profiles/{profile.id}/reset")
        assert resp.status_code == 200
        data = resp.json()
        assert data["description"] == "Raspberry Pi Zero 2 W — H.264 Main, 1080p30"
        assert data["video_profile"] == "main"
        assert data["max_width"] == 1920
        assert data["max_height"] == 1080
        assert data["max_fps"] == 30
        assert data["crf"] == 23
        assert data["audio_codec"] == "aac"
        assert data["audio_bitrate"] == "128k"

    async def test_reset_non_builtin_returns_400(self, client, db_session):
        """Resetting a non-built-in profile should return 400."""
        profile = DeviceProfile(
            name="custom-profile", video_codec="h264",
            video_profile="main", builtin=False,
        )
        db_session.add(profile)
        await db_session.commit()

        resp = await client.post(f"/api/profiles/{profile.id}/reset")
        assert resp.status_code == 400
        assert "built-in" in resp.json()["detail"].lower()

    async def test_reset_nonexistent_returns_404(self, client):
        """Resetting a nonexistent profile should return 404."""
        import uuid
        resp = await client.post(f"/api/profiles/{uuid.uuid4()}/reset")
        assert resp.status_code == 404

    async def test_reset_triggers_retranscode(self, client, db_session):
        """Reset that changes transcode fields should reset variants to PENDING."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus

        profile = DeviceProfile(
            name="pi-4",
            description="Custom",
            video_codec="h265",
            video_profile="high",  # default is "main"
            max_width=1280,
            max_height=720,
            max_fps=24,
            crf=18,
            audio_codec="mp3",
            audio_bitrate="64k",
            builtin=True,
        )
        db_session.add(profile)
        await db_session.commit()

        asset = Asset(
            filename="test.mp4",
            original_filename="test.mp4",
            size_bytes=1000,
            asset_type=AssetType.VIDEO,
            duration_seconds=10.0,
        )
        db_session.add(asset)
        await db_session.commit()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename="test_variant.mp4",
            status=VariantStatus.READY,
        )
        db_session.add(variant)
        await db_session.commit()

        resp = await client.post(f"/api/profiles/{profile.id}/reset")
        assert resp.status_code == 200

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING

    async def test_reset_no_retranscode_when_unchanged(self, client, db_session):
        """Reset on a builtin already at defaults should not retranscode."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus

        profile = DeviceProfile(
            name="pi-5",
            description="Raspberry Pi 5 / CM5 — HEVC Main, 1080p60",
            video_codec="h265",
            video_profile="main",
            max_width=1920,
            max_height=1080,
            max_fps=60,
            crf=23,
            video_bitrate="",
            pixel_format="auto",
            color_space="auto",
            audio_codec="aac",
            audio_bitrate="128k",
            builtin=True,
        )
        db_session.add(profile)
        await db_session.commit()

        asset = Asset(
            filename="test2.mp4",
            original_filename="test2.mp4",
            size_bytes=1000,
            asset_type=AssetType.VIDEO,
            duration_seconds=10.0,
        )
        db_session.add(asset)
        await db_session.commit()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename="test2_variant.mp4",
            status=VariantStatus.READY,
        )
        db_session.add(variant)
        await db_session.commit()

        resp = await client.post(f"/api/profiles/{profile.id}/reset")
        assert resp.status_code == 200

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.READY  # unchanged
