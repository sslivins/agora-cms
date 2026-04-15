"""Tests for image variant format handling.

Ensures image assets produce correct file formats in variants:
- JPEG source → JPEG variant (.jpg)
- PNG source → PNG variant (.png)
- HEIC source (with original) → variant from original HEIC, not intermediate JPEG

Covers issue #97: image variants were incorrectly created as MP4 containers
instead of the proper image format.
"""

import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device_profile import DeviceProfile


@pytest_asyncio.fixture
async def profile(db_session):
    """Create a test profile with 1280x720 max dimensions."""
    p = DeviceProfile(name="Test Profile", max_width=1280, max_height=720)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest_asyncio.fixture
async def second_profile(db_session):
    """Create a second test profile with different dimensions."""
    p = DeviceProfile(name="Small Profile", max_width=640, max_height=480)
    db_session.add(p)
    await db_session.commit()
    await db_session.refresh(p)
    return p


@pytest.mark.asyncio
class TestEnqueueVariantExtension:
    """Variant filename extension should match the source image format."""

    async def test_jpeg_asset_gets_jpg_variant(self, db_session, profile):
        """JPEG source asset should produce a .jpg variant."""
        asset = Asset(
            filename="photo.jpg", asset_type=AssetType.IMAGE,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.commit()

        from cms.routers.assets import _enqueue_transcoding
        await _enqueue_transcoding(asset, db_session)

        variants = (await db_session.execute(
            __import__("sqlalchemy").select(AssetVariant)
        )).scalars().all()
        assert len(variants) == 1
        assert variants[0].filename.endswith(".jpg")

    async def test_png_asset_gets_png_variant(self, db_session, profile):
        """PNG source asset should produce a .png variant, not .jpg."""
        asset = Asset(
            filename="logo.png", asset_type=AssetType.IMAGE,
            size_bytes=2000, checksum="def",
        )
        db_session.add(asset)
        await db_session.commit()

        from cms.routers.assets import _enqueue_transcoding
        await _enqueue_transcoding(asset, db_session)

        variants = (await db_session.execute(
            __import__("sqlalchemy").select(AssetVariant)
        )).scalars().all()
        assert len(variants) == 1
        assert variants[0].filename.endswith(".png"), \
            f"PNG asset should get .png variant, got {variants[0].filename}"

    async def test_heic_converted_asset_gets_jpg_variant(self, db_session, profile):
        """HEIC source (stored as .jpg after upload conversion) gets .jpg variant."""
        asset = Asset(
            filename="photo.jpg", original_filename="photo.heic",
            asset_type=AssetType.IMAGE,
            size_bytes=3000, checksum="ghi",
        )
        db_session.add(asset)
        await db_session.commit()

        from cms.routers.assets import _enqueue_transcoding
        await _enqueue_transcoding(asset, db_session)

        variants = (await db_session.execute(
            __import__("sqlalchemy").select(AssetVariant)
        )).scalars().all()
        assert len(variants) == 1
        assert variants[0].filename.endswith(".jpg")

    async def test_video_asset_gets_mp4_variant(self, db_session, profile):
        """Video assets should still get .mp4 variants (sanity check)."""
        asset = Asset(
            filename="clip.mp4", asset_type=AssetType.VIDEO,
            size_bytes=50000, checksum="vid",
        )
        db_session.add(asset)
        await db_session.commit()

        from cms.routers.assets import _enqueue_transcoding
        await _enqueue_transcoding(asset, db_session)

        variants = (await db_session.execute(
            __import__("sqlalchemy").select(AssetVariant)
        )).scalars().all()
        assert len(variants) == 1
        assert variants[0].filename.endswith(".mp4")


@pytest.mark.asyncio
class TestEnqueueForNewProfileExtension:
    """enqueue_for_new_profile should also assign correct image extensions."""

    async def test_png_asset_gets_png_variant_new_profile(self, db_session):
        """PNG source should produce .png variant when a new profile is created."""
        asset = Asset(
            filename="splash.png", asset_type=AssetType.IMAGE,
            size_bytes=4000, checksum="png1",
        )
        db_session.add(asset)
        await db_session.commit()

        profile = DeviceProfile(name="New Profile", max_width=1920, max_height=1080)
        db_session.add(profile)
        await db_session.commit()

        from cms.services.transcoder import enqueue_for_new_profile
        count = await enqueue_for_new_profile(profile.id, db_session)
        assert count == 1

        variants = (await db_session.execute(
            __import__("sqlalchemy").select(AssetVariant)
        )).scalars().all()
        assert len(variants) == 1
        assert variants[0].filename.endswith(".png"), \
            f"PNG asset should get .png variant, got {variants[0].filename}"

    async def test_jpg_asset_gets_jpg_variant_new_profile(self, db_session):
        """JPG source should produce .jpg variant when a new profile is created."""
        asset = Asset(
            filename="photo.jpg", asset_type=AssetType.IMAGE,
            size_bytes=3000, checksum="jpg1",
        )
        db_session.add(asset)
        await db_session.commit()

        profile = DeviceProfile(name="New Profile 2", max_width=1920, max_height=1080)
        db_session.add(profile)
        await db_session.commit()

        from cms.services.transcoder import enqueue_for_new_profile
        count = await enqueue_for_new_profile(profile.id, db_session)
        assert count == 1

        variants = (await db_session.execute(
            __import__("sqlalchemy").select(AssetVariant)
        )).scalars().all()
        assert len(variants) == 1
        assert variants[0].filename.endswith(".jpg")


@pytest.mark.asyncio
class TestTranscodeImageSourcePath:
    """_transcode_one should use the original source file when available."""

    async def test_uses_original_heic_for_variant(self, db_session, profile, tmp_path):
        """When asset has original_filename, variant should be transcoded
        from originals/ dir, not the intermediate JPEG."""
        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        originals_dir = asset_dir / "originals"
        originals_dir.mkdir()

        # Create both files: intermediate JPEG and original HEIC
        jpg_path = asset_dir / "photo.jpg"
        jpg_path.write_bytes(b"fake-jpeg-data")
        heic_path = originals_dir / "photo.heic"
        heic_path.write_bytes(b"fake-heic-data")

        asset = Asset(
            filename="photo.jpg", original_filename="photo.heic",
            asset_type=AssetType.IMAGE,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.commit()

        variant_id = uuid.uuid4()
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{variant_id}.jpg",
            status=VariantStatus.PENDING,
        )
        db_session.add(variant)
        await db_session.commit()

        # Mock convert_image to capture which source path is used
        captured_source = []

        async def mock_convert(source_path, output_path, **kwargs):
            captured_source.append(source_path)
            output_path.write_bytes(b"fake-output")
            return True

        with patch("worker.transcoder.convert_image", side_effect=mock_convert), \
             patch("worker.transcoder.probe_media", return_value={}):
            from worker.transcoder import _transcode_one
            await _transcode_one(variant, db_session, asset_dir)

        assert len(captured_source) == 1
        assert captured_source[0] == heic_path, \
            f"Should use original HEIC, not intermediate JPEG. Got: {captured_source[0]}"

    async def test_uses_direct_source_when_no_original(self, db_session, profile, tmp_path):
        """When no original_filename, variant uses the asset file directly."""
        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()

        jpg_path = asset_dir / "photo.jpg"
        jpg_path.write_bytes(b"fake-jpeg-data")

        asset = Asset(
            filename="photo.jpg", original_filename=None,
            asset_type=AssetType.IMAGE,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.commit()

        variant_id = uuid.uuid4()
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{variant_id}.jpg",
            status=VariantStatus.PENDING,
        )
        db_session.add(variant)
        await db_session.commit()

        captured_source = []

        async def mock_convert(source_path, output_path, **kwargs):
            captured_source.append(source_path)
            output_path.write_bytes(b"fake-output")
            return True

        with patch("worker.transcoder.convert_image", side_effect=mock_convert), \
             patch("worker.transcoder.probe_media", return_value={}):
            from worker.transcoder import _transcode_one
            await _transcode_one(variant, db_session, asset_dir)

        assert len(captured_source) == 1
        assert captured_source[0] == jpg_path


@pytest.mark.asyncio
class TestTranscodeImageOutputFormat:
    """_transcode_one should output the correct image format."""

    async def test_image_variant_output_is_jpg(self, db_session, profile, tmp_path):
        """Image variant with .jpg filename should produce actual JPEG output."""
        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()

        jpg_path = asset_dir / "photo.jpg"
        jpg_path.write_bytes(b"fake-jpeg-data")

        asset = Asset(
            filename="photo.jpg", asset_type=AssetType.IMAGE,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.commit()

        variant_id = uuid.uuid4()
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{variant_id}.jpg",
            status=VariantStatus.PENDING,
        )
        db_session.add(variant)
        await db_session.commit()

        captured_output = []

        async def mock_convert(source_path, output_path, **kwargs):
            captured_output.append(output_path)
            output_path.write_bytes(b"fake-output")
            return True

        with patch("worker.transcoder.convert_image", side_effect=mock_convert), \
             patch("worker.transcoder.probe_media", return_value={}):
            from worker.transcoder import _transcode_one
            await _transcode_one(variant, db_session, asset_dir)

        assert len(captured_output) == 1
        assert captured_output[0].suffix == ".jpg", \
            f"Output path should be .jpg, got {captured_output[0].suffix}"

    async def test_png_variant_calls_convert_image_to_png(self, db_session, profile, tmp_path):
        """PNG variant should call convert_image_to_png, not convert_image_to_jpeg."""
        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()

        png_path = asset_dir / "logo.png"
        png_path.write_bytes(b"fake-png-data")

        asset = Asset(
            filename="logo.png", asset_type=AssetType.IMAGE,
            size_bytes=2000, checksum="def",
        )
        db_session.add(asset)
        await db_session.commit()

        variant_id = uuid.uuid4()
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{variant_id}.png",
            status=VariantStatus.PENDING,
        )
        db_session.add(variant)
        await db_session.commit()

        captured_output = []

        async def mock_convert(source_path, output_path, **kwargs):
            captured_output.append(output_path)
            output_path.write_bytes(b"fake-output")
            return True

        with patch("worker.transcoder.convert_image", side_effect=mock_convert), \
             patch("worker.transcoder.probe_media", return_value={}):
            from worker.transcoder import _transcode_one
            await _transcode_one(variant, db_session, asset_dir)

        assert len(captured_output) == 1
        assert captured_output[0].suffix == ".png", \
            f"Output path should be .png, got {captured_output[0].suffix}"

    async def test_mp4_extension_variant_still_outputs_jpg(self, db_session, profile, tmp_path):
        """Legacy .mp4 extension on image variant should still produce correct
        image output (resilience against stale DB data)."""
        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()

        jpg_path = asset_dir / "old_photo.jpg"
        jpg_path.write_bytes(b"fake-jpeg-data")

        asset = Asset(
            filename="old_photo.jpg", asset_type=AssetType.IMAGE,
            size_bytes=1000, checksum="legacy",
        )
        db_session.add(asset)
        await db_session.commit()

        variant_id = uuid.uuid4()
        # Simulate legacy variant with wrong .mp4 extension
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{variant_id}.mp4",
            status=VariantStatus.PENDING,
        )
        db_session.add(variant)
        await db_session.commit()

        captured_output = []

        async def mock_convert(source_path, output_path, **kwargs):
            captured_output.append(output_path)
            output_path.write_bytes(b"fake-output")
            return True

        with patch("worker.transcoder.convert_image", side_effect=mock_convert), \
             patch("worker.transcoder.probe_media", return_value={}):
            from worker.transcoder import _transcode_one
            await _transcode_one(variant, db_session, asset_dir)

        assert len(captured_output) == 1
        # Even though DB says .mp4, the actual output should be image format
        assert captured_output[0].suffix == ".jpg", \
            f"Image variant output should be .jpg even with legacy .mp4 filename, got {captured_output[0].suffix}"


@pytest.mark.asyncio
class TestStartupMigrationFixesBrokenVariants:
    """Startup should detect and re-queue image variants with .mp4 extension."""

    async def test_mp4_image_variants_reset_to_pending(self, db_session):
        """Image variants with .mp4 extension should be reset to PENDING
        with corrected filename on startup."""
        from cms.services.transcoder import fix_image_variant_extensions

        profile = DeviceProfile(name="Startup Test")
        db_session.add(profile)
        await db_session.commit()

        asset = Asset(
            filename="photo.jpg", asset_type=AssetType.IMAGE,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.commit()

        variant_id = uuid.uuid4()
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{variant_id}.mp4",
            status=VariantStatus.READY,
            size_bytes=50000,
        )
        db_session.add(variant)
        await db_session.commit()

        fixed = await fix_image_variant_extensions(db_session)
        assert fixed == 1

        await db_session.refresh(variant)
        assert variant.filename.endswith(".jpg"), \
            f"Should be renamed to .jpg, got {variant.filename}"
        assert variant.status == VariantStatus.PENDING
        assert variant.size_bytes == 0

    async def test_correct_jpg_variants_not_touched(self, db_session):
        """Image variants already using .jpg should not be affected."""
        from cms.services.transcoder import fix_image_variant_extensions

        profile = DeviceProfile(name="OK Test")
        db_session.add(profile)
        await db_session.commit()

        asset = Asset(
            filename="photo.jpg", asset_type=AssetType.IMAGE,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.commit()

        variant_id = uuid.uuid4()
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{variant_id}.jpg",
            status=VariantStatus.READY,
            size_bytes=30000,
        )
        db_session.add(variant)
        await db_session.commit()

        fixed = await fix_image_variant_extensions(db_session)
        assert fixed == 0

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.READY
        assert variant.size_bytes == 30000

    async def test_video_mp4_variants_not_touched(self, db_session):
        """Video variants with .mp4 should not be affected by the migration."""
        from cms.services.transcoder import fix_image_variant_extensions

        profile = DeviceProfile(name="Video Test")
        db_session.add(profile)
        await db_session.commit()

        asset = Asset(
            filename="clip.mp4", asset_type=AssetType.VIDEO,
            size_bytes=50000, checksum="vid",
        )
        db_session.add(asset)
        await db_session.commit()

        variant_id = uuid.uuid4()
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{variant_id}.mp4",
            status=VariantStatus.READY,
            size_bytes=40000,
        )
        db_session.add(variant)
        await db_session.commit()

        fixed = await fix_image_variant_extensions(db_session)
        assert fixed == 0

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.READY
        assert variant.filename.endswith(".mp4")

    async def test_png_asset_mp4_variant_fixed_to_png(self, db_session):
        """PNG asset with .mp4 variant should be fixed to .png, not .jpg."""
        from cms.services.transcoder import fix_image_variant_extensions

        profile = DeviceProfile(name="PNG Fix Test")
        db_session.add(profile)
        await db_session.commit()

        asset = Asset(
            filename="splash.png", asset_type=AssetType.IMAGE,
            size_bytes=4000, checksum="png1",
        )
        db_session.add(asset)
        await db_session.commit()

        variant_id = uuid.uuid4()
        variant = AssetVariant(
            id=variant_id,
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{variant_id}.mp4",
            status=VariantStatus.READY,
            size_bytes=30000,
        )
        db_session.add(variant)
        await db_session.commit()

        fixed = await fix_image_variant_extensions(db_session)
        assert fixed == 1

        await db_session.refresh(variant)
        assert variant.filename.endswith(".png"), \
            f"PNG asset variant should be fixed to .png, got {variant.filename}"
        assert variant.status == VariantStatus.PENDING
