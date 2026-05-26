"""Tests for the CMS-internal ``thumbnail`` device profile.

The thumbnail profile is purpose=``thumbnail`` and backs the asset
library grid view. It must:
  - be hidden from ``GET /api/profiles`` by default
  - reject edit/delete/copy/disable/enable/reset via the public API
  - cause new variants to have a ``.jpg`` extension regardless of
    source type
  - be filtered out of device-bound profile dropdowns in the UI
  - populate ``AssetOut.thumbnail_url`` once the variant is READY
"""

import uuid

import pytest
from sqlalchemy import select

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device_profile import DeviceProfile
from cms.services.transcoder import (
    _variant_ext_for,
    enqueue_for_new_profile,
)


@pytest.mark.asyncio
class TestThumbnailProfileExposure:
    """The thumbnail profile is CMS-internal and hidden from public APIs."""

    async def _make_thumbnail(self, db_session):
        p = DeviceProfile(
            name="test-thumb",
            video_codec="h264",
            video_profile="main",
            max_width=480,
            max_height=480,
            max_fps=1,
            builtin=True,
            purpose="thumbnail",
        )
        db_session.add(p)
        await db_session.commit()
        return p

    async def test_list_hides_thumbnail_by_default(self, client, db_session):
        thumb = await self._make_thumbnail(db_session)
        resp = await client.get("/api/profiles")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()]
        assert thumb.name not in names

    async def test_list_includes_thumbnail_when_asked(self, client, db_session):
        thumb = await self._make_thumbnail(db_session)
        resp = await client.get("/api/profiles?include_internal=true")
        assert resp.status_code == 200
        names = [p["name"] for p in resp.json()]
        assert thumb.name in names
        for p in resp.json():
            if p["name"] == thumb.name:
                assert p["purpose"] == "thumbnail"

    async def test_edit_thumbnail_rejected(self, client, db_session):
        thumb = await self._make_thumbnail(db_session)
        resp = await client.put(
            f"/api/profiles/{thumb.id}",
            json={"description": "nope"},
        )
        assert resp.status_code == 400
        assert "internal" in resp.json()["detail"].lower()

    async def test_delete_thumbnail_rejected(self, client, db_session):
        thumb = await self._make_thumbnail(db_session)
        resp = await client.delete(f"/api/profiles/{thumb.id}")
        assert resp.status_code == 400

    async def test_copy_thumbnail_rejected(self, client, db_session):
        thumb = await self._make_thumbnail(db_session)
        resp = await client.post(f"/api/profiles/{thumb.id}/copy")
        assert resp.status_code == 400

    async def test_disable_thumbnail_rejected(self, client, db_session):
        thumb = await self._make_thumbnail(db_session)
        resp = await client.post(f"/api/profiles/{thumb.id}/disable")
        assert resp.status_code == 400

    async def test_reset_thumbnail_rejected(self, client, db_session):
        thumb = await self._make_thumbnail(db_session)
        resp = await client.post(f"/api/profiles/{thumb.id}/reset")
        assert resp.status_code == 400


class TestVariantExtension:
    """Thumbnail-purpose profiles always emit .jpg, regardless of source."""

    def _asset(self, asset_type: AssetType, filename: str) -> Asset:
        return Asset(
            id=uuid.uuid4(),
            filename=filename,
            asset_type=asset_type,
            size_bytes=1,
            checksum="x",
        )

    def _profile(self, *, purpose: str, audio_codec: str = "aac") -> DeviceProfile:
        return DeviceProfile(
            name="x",
            video_codec="h264",
            video_profile="main",
            audio_codec=audio_codec,
            purpose=purpose,
        )

    def test_thumbnail_video_is_jpg(self):
        ext = _variant_ext_for(
            self._asset(AssetType.VIDEO, "clip.mp4"),
            self._profile(purpose="thumbnail"),
        )
        assert ext == ".jpg"

    def test_thumbnail_png_image_is_jpg(self):
        # Thumbnail profile drops transparency on purpose - always .jpg.
        ext = _variant_ext_for(
            self._asset(AssetType.IMAGE, "logo.png"),
            self._profile(purpose="thumbnail"),
        )
        assert ext == ".jpg"

    def test_device_png_image_keeps_png(self):
        ext = _variant_ext_for(
            self._asset(AssetType.IMAGE, "logo.png"),
            self._profile(purpose="device"),
        )
        assert ext == ".png"

    def test_device_video_is_mp4(self):
        ext = _variant_ext_for(
            self._asset(AssetType.VIDEO, "clip.mp4"),
            self._profile(purpose="device"),
        )
        assert ext == ".mp4"

    def test_device_libopus_is_mkv(self):
        ext = _variant_ext_for(
            self._asset(AssetType.VIDEO, "clip.mp4"),
            self._profile(purpose="device", audio_codec="libopus"),
        )
        assert ext == ".mkv"


@pytest.mark.asyncio
class TestThumbnailBackfill:
    """Adding a thumbnail profile to a populated CMS backfills variants
    for every existing video + image asset, each with a .jpg filename."""

    async def test_enqueue_creates_jpg_variants(self, db_session):
        # Seed two assets - one image, one video.
        img = Asset(
            id=uuid.uuid4(), filename="pic.jpg", asset_type=AssetType.IMAGE,
            size_bytes=1, checksum="a",
        )
        vid = Asset(
            id=uuid.uuid4(), filename="clip.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1, checksum="b",
        )
        db_session.add_all([img, vid])

        thumb = DeviceProfile(
            name="thumb-bf",
            video_codec="h264", video_profile="main",
            max_width=480, max_height=480, max_fps=1,
            builtin=True, purpose="thumbnail",
        )
        db_session.add(thumb)
        await db_session.commit()

        new_ids = await enqueue_for_new_profile(thumb.id, db_session)
        assert len(new_ids) == 2

        rows = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.profile_id == thumb.id)
        )).scalars().all()
        assert len(rows) == 2
        assert all(v.filename.endswith(".jpg") for v in rows), (
            "thumbnail variants must always end in .jpg, even for PNG/MP4 sources"
        )


@pytest.mark.asyncio
class TestAssetOutThumbnailUrl:
    """``AssetOut.thumbnail_url`` is populated once the thumbnail variant
    reaches READY and is None otherwise."""

    async def test_no_thumb_yields_null(self, client, db_session):
        a = Asset(
            id=uuid.uuid4(), filename="x.jpg", asset_type=AssetType.IMAGE,
            size_bytes=1, checksum="c",
        )
        db_session.add(a)
        await db_session.commit()

        resp = await client.get(f"/api/assets/{a.id}")
        assert resp.status_code == 200
        assert resp.json()["thumbnail_url"] is None

    async def test_ready_variant_yields_url(self, client, db_session):
        a = Asset(
            id=uuid.uuid4(), filename="x.jpg", asset_type=AssetType.IMAGE,
            size_bytes=1, checksum="d",
        )
        thumb = DeviceProfile(
            name="thumb-url",
            video_codec="h264", video_profile="main",
            max_width=480, max_height=480, max_fps=1,
            builtin=True, purpose="thumbnail",
        )
        db_session.add_all([a, thumb])
        await db_session.flush()

        v = AssetVariant(
            id=uuid.uuid4(),
            source_asset_id=a.id,
            profile_id=thumb.id,
            filename=f"{uuid.uuid4()}.jpg",
            status=VariantStatus.READY,
        )
        db_session.add(v)
        await db_session.commit()

        resp = await client.get(f"/api/assets/{a.id}")
        assert resp.status_code == 200
        assert resp.json()["thumbnail_url"] == f"/api/assets/variants/{v.id}/preview"
