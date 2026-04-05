"""Tests for UUID-based variant filenames and profile name validation."""

import re
import uuid

import pytest
import pytest_asyncio


_UUID_FILENAME = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.mp4$"
)


@pytest.mark.asyncio
class TestVariantUUIDFilenames:
    """New variants should use {uuid}.mp4 filenames, not profile-name-based."""

    async def test_enqueue_transcoding_produces_uuid_filename(self, db_session):
        """_enqueue_transcoding should create variants with UUID filenames."""
        from cms.models.asset import Asset, AssetType, AssetVariant
        from cms.models.device_profile import DeviceProfile
        from cms.routers.assets import _enqueue_transcoding

        profile = DeviceProfile(name="pi-zero-2w", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="my_video.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        await _enqueue_transcoding(asset, db_session)

        from sqlalchemy import select
        result = await db_session.execute(select(AssetVariant))
        variants = result.scalars().all()
        assert len(variants) == 1

        v = variants[0]
        assert _UUID_FILENAME.match(v.filename), f"Expected UUID filename, got: {v.filename}"
        assert v.filename == f"{v.id}.mp4"

    async def test_enqueue_for_new_profile_produces_uuid_filename(self, db_session):
        """enqueue_for_new_profile should create variants with UUID filenames."""
        from cms.models.asset import Asset, AssetType, AssetVariant
        from cms.models.device_profile import DeviceProfile
        from cms.services.transcoder import enqueue_for_new_profile

        asset = Asset(
            filename="test_clip.mp4", asset_type=AssetType.VIDEO,
            size_bytes=500, checksum="def",
        )
        db_session.add(asset)
        await db_session.flush()

        profile = DeviceProfile(name="test-profile", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()

        count = await enqueue_for_new_profile(profile.id, db_session)
        assert count == 1

        from sqlalchemy import select
        result = await db_session.execute(select(AssetVariant))
        v = result.scalar_one()
        assert _UUID_FILENAME.match(v.filename), f"Expected UUID filename, got: {v.filename}"
        assert v.filename == f"{v.id}.mp4"

    async def test_variant_filename_not_based_on_profile_name(self, db_session):
        """Variant filename must NOT contain the profile name."""
        from cms.models.asset import Asset, AssetType, AssetVariant
        from cms.models.device_profile import DeviceProfile
        from cms.routers.assets import _enqueue_transcoding

        profile = DeviceProfile(name="my-special-profile", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="source.mp4", asset_type=AssetType.VIDEO,
            size_bytes=100, checksum="ghi",
        )
        db_session.add(asset)
        await db_session.flush()

        await _enqueue_transcoding(asset, db_session)

        from sqlalchemy import select
        v = (await db_session.execute(select(AssetVariant))).scalar_one()
        assert "my-special-profile" not in v.filename
        assert "source" not in v.filename


@pytest.mark.asyncio
class TestProfileNameValidation:
    """Profile names should be restricted to filesystem-safe characters."""

    async def test_valid_profile_names(self, client):
        valid_names = ["pi-zero-2w", "rpi4_1080p", "Test123", "a"]
        for name in valid_names:
            resp = await client.post("/api/profiles", json={"name": name})
            assert resp.status_code == 201, f"Name '{name}' should be valid, got {resp.status_code}"
            # Clean up — delete by ID
            profile_id = resp.json()["id"]
            await client.delete(f"/api/profiles/{profile_id}")

    async def test_profile_name_rejects_spaces(self, client):
        resp = await client.post("/api/profiles", json={"name": "my profile"})
        assert resp.status_code == 422

    async def test_profile_name_rejects_special_chars(self, client):
        for bad_name in ["test/profile", "test@profile", "test profile", "../escape", ""]:
            resp = await client.post("/api/profiles", json={"name": bad_name})
            assert resp.status_code == 422, f"Name '{bad_name}' should be rejected"

    async def test_profile_name_rejects_leading_hyphen(self, client):
        resp = await client.post("/api/profiles", json={"name": "-leading"})
        assert resp.status_code == 422


@pytest.mark.asyncio
class TestProfileUpdateImmutableName:
    """Profile name must not be changeable via update."""

    async def test_update_ignores_name_field(self, client, db_session):
        """PUT with a name field should not change the profile name."""
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="original-name", video_codec="h264")
        db_session.add(profile)
        await db_session.commit()

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"name": "new-name", "description": "updated"},
        )
        assert resp.status_code == 200
        data = resp.json()
        # Name should remain unchanged — name is not in ProfileUpdate
        assert data["name"] == "original-name"
        assert data["description"] == "updated"


@pytest.mark.asyncio
class TestVariantFilenameMigration:
    """Startup migration should rename legacy variant files to UUID scheme."""

    async def test_migrates_legacy_filename(self, db_session, tmp_path):
        """Variants with non-UUID filenames should be renamed on startup."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile
        from cms.config import Settings

        profile = DeviceProfile(name="pi-zero-2w", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="video.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="aaa",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id, profile_id=profile.id,
            filename="video_pi-zero-2w.mp4",  # legacy name
            status=VariantStatus.READY, size_bytes=500, checksum="bbb",
        )
        db_session.add(variant)
        await db_session.commit()

        # Create the legacy file on disk
        variants_dir = tmp_path / "assets" / "variants"
        variants_dir.mkdir(parents=True)
        legacy_file = variants_dir / "video_pi-zero-2w.mp4"
        legacy_file.write_bytes(b"fake video data")

        settings = Settings(
            database_url="sqlite+aiosqlite://",
            asset_storage_path=tmp_path / "assets",
        )

        from cms.main import _migrate_variant_filenames
        await _migrate_variant_filenames(db_session, settings)

        await db_session.refresh(variant)
        expected = f"{variant.id}.mp4"
        assert variant.filename == expected
        assert (variants_dir / expected).is_file()
        assert not legacy_file.exists()

    async def test_skips_already_uuid_filename(self, db_session, tmp_path):
        """Variants that already have UUID filenames should not be touched."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile
        from cms.config import Settings

        profile = DeviceProfile(name="pi-zero-2w", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="video.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="aaa",
        )
        db_session.add(asset)
        await db_session.flush()

        vid = uuid.uuid4()
        variant = AssetVariant(
            id=vid,
            source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{vid}.mp4",  # already UUID
            status=VariantStatus.READY, size_bytes=500, checksum="bbb",
        )
        db_session.add(variant)
        await db_session.commit()

        variants_dir = tmp_path / "assets" / "variants"
        variants_dir.mkdir(parents=True)
        (variants_dir / f"{vid}.mp4").write_bytes(b"data")

        settings = Settings(
            database_url="sqlite+aiosqlite://",
            asset_storage_path=tmp_path / "assets",
        )

        from cms.main import _migrate_variant_filenames
        await _migrate_variant_filenames(db_session, settings)

        await db_session.refresh(variant)
        assert variant.filename == f"{vid}.mp4"
        assert (variants_dir / f"{vid}.mp4").is_file()
