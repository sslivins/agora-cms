"""Tests for asset API endpoints."""

import io

import pytest


def _make_upload(filename: str, content: bytes = b"fakecontent"):
    return {"file": (filename, io.BytesIO(content), "application/octet-stream")}


@pytest.mark.asyncio
class TestAssetStatus:
    async def test_status_empty(self, client):
        resp = await client.get("/api/assets/status")
        assert resp.status_code == 200
        data = resp.json()
        assert data["asset_count"] == 0
        assert data["variant_ready"] == 0
        assert data["variant_processing"] == 0
        assert data["variant_failed"] == 0
        assert data["assets"] == []

    async def test_status_returns_per_asset_variants(self, client, db_session):
        """Status endpoint should include per-asset variant details."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="Pi Zero Test")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="status_test.mp4", asset_type=AssetType.VIDEO,
            size_bytes=5000, checksum="abc123",
        )
        db_session.add(asset)
        await db_session.flush()

        # Add variants in different states
        v_ready = AssetVariant(
            source_asset_id=asset.id, profile_id=profile.id,
            filename="status_test_pizero.mp4", size_bytes=3000,
            status=VariantStatus.READY, progress=100.0, checksum="def",
            width=1280, height=720, video_codec="h264", bitrate=5000000,
            frame_rate="30",
        )
        v_processing = AssetVariant(
            source_asset_id=asset.id, profile_id=profile.id,
            filename="status_test_pizero2.mp4", size_bytes=0,
            status=VariantStatus.PROCESSING, progress=45.0, checksum="",
        )
        db_session.add_all([v_ready, v_processing])
        await db_session.commit()

        resp = await client.get("/api/assets/status")
        assert resp.status_code == 200
        data = resp.json()

        assert data["asset_count"] == 1
        assert data["variant_ready"] == 1
        assert data["variant_processing"] == 1
        assert len(data["assets"]) == 1

        asset_data = data["assets"][0]
        assert asset_data["id"] == str(asset.id)
        assert asset_data["variant_total"] == 2
        assert asset_data["variant_ready"] == 1
        assert asset_data["variant_processing"] == 1
        assert len(asset_data["variants"]) == 2

        # Find the processing variant and verify fields
        proc_variant = [v for v in asset_data["variants"] if v["status"] == "processing"][0]
        assert proc_variant["progress"] == 45.0
        assert proc_variant["profile_name"] == "Pi Zero Test"

        # Find the ready variant and verify metadata
        ready_variant = [v for v in asset_data["variants"] if v["status"] == "ready"][0]
        assert ready_variant["width"] == 1280
        assert ready_variant["height"] == 720
        assert ready_variant["video_codec"] == "h264"
        assert ready_variant["size_bytes"] == 3000


@pytest.mark.asyncio
class TestAssetUpload:
    async def test_upload_mp4(self, client):
        resp = await client.post("/api/assets/upload", files=_make_upload("test.mp4"))
        assert resp.status_code == 201
        data = resp.json()
        assert data["filename"] == "test.mp4"
        assert data["asset_type"] == "video"
        assert data["size_bytes"] == 11

    async def test_upload_image(self, client):
        resp = await client.post("/api/assets/upload", files=_make_upload("photo.jpg"))
        assert resp.status_code == 201
        assert resp.json()["asset_type"] == "image"

    async def test_upload_png(self, client):
        resp = await client.post("/api/assets/upload", files=_make_upload("slide.png"))
        assert resp.status_code == 201
        assert resp.json()["asset_type"] == "image"

    async def test_upload_invalid_extension(self, client):
        resp = await client.post("/api/assets/upload", files=_make_upload("hack.exe"))
        assert resp.status_code == 400

    async def test_upload_invalid_filename_chars(self, client):
        resp = await client.post("/api/assets/upload", files=_make_upload("../etc/passwd.mp4"))
        assert resp.status_code == 400

    async def test_upload_duplicate(self, client):
        await client.post("/api/assets/upload", files=_make_upload("dup.mp4"))
        resp = await client.post("/api/assets/upload", files=_make_upload("dup.mp4"))
        assert resp.status_code == 409

    async def test_upload_requires_auth(self, unauthed_client):
        resp = await unauthed_client.post("/api/assets/upload", files=_make_upload("x.mp4"))
        assert resp.status_code in (401, 303)


@pytest.mark.asyncio
class TestAssetList:
    async def test_list_empty(self, client):
        resp = await client.get("/api/assets")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_list_after_upload(self, client):
        await client.post("/api/assets/upload", files=_make_upload("vid1.mp4"))
        await client.post("/api/assets/upload", files=_make_upload("vid2.mp4"))

        resp = await client.get("/api/assets")
        assert resp.status_code == 200
        assert len(resp.json()) == 2


@pytest.mark.asyncio
class TestAssetGetAndDelete:
    async def test_get_asset(self, client):
        upload = await client.post("/api/assets/upload", files=_make_upload("get-me.mp4"))
        asset_id = upload.json()["id"]

        resp = await client.get(f"/api/assets/{asset_id}")
        assert resp.status_code == 200
        assert resp.json()["filename"] == "get-me.mp4"

    async def test_get_nonexistent(self, client):
        resp = await client.get("/api/assets/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404

    async def test_delete_asset(self, client):
        upload = await client.post("/api/assets/upload", files=_make_upload("del-me.mp4"))
        asset_id = upload.json()["id"]

        resp = await client.delete(f"/api/assets/{asset_id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "del-me.mp4"

        # Verify gone
        resp = await client.get(f"/api/assets/{asset_id}")
        assert resp.status_code == 404

    async def test_delete_nonexistent(self, client):
        resp = await client.delete("/api/assets/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestAssetChecksum:
    async def test_checksum_is_sha256(self, client):
        content = b"deterministic content"
        resp = await client.post("/api/assets/upload", files={"file": ("check.mp4", io.BytesIO(content), "application/octet-stream")})
        assert resp.status_code == 201
        import hashlib
        expected = hashlib.sha256(content).hexdigest()
        assert resp.json()["checksum"] == expected


@pytest.mark.asyncio
class TestAssetPreview:
    async def test_preview_image(self, client):
        content = b"fake-png-content"
        upload = await client.post("/api/assets/upload", files={"file": ("pic.png", io.BytesIO(content), "application/octet-stream")})
        asset_id = upload.json()["id"]

        resp = await client.get(f"/api/assets/{asset_id}/preview")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/png"
        assert resp.content == content

    async def test_preview_jpeg(self, client):
        content = b"fake-jpg-content"
        upload = await client.post("/api/assets/upload", files={"file": ("photo.jpg", io.BytesIO(content), "application/octet-stream")})
        asset_id = upload.json()["id"]

        resp = await client.get(f"/api/assets/{asset_id}/preview")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "image/jpeg"

    async def test_preview_video(self, client):
        content = b"fake-mp4-content"
        upload = await client.post("/api/assets/upload", files={"file": ("clip.mp4", io.BytesIO(content), "application/octet-stream")})
        asset_id = upload.json()["id"]

        resp = await client.get(f"/api/assets/{asset_id}/preview")
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "video/mp4"
        assert resp.content == content

    async def test_preview_nonexistent(self, client):
        resp = await client.get("/api/assets/00000000-0000-0000-0000-000000000000/preview")
        assert resp.status_code == 404

    async def test_preview_requires_auth(self, unauthed_client, client):
        upload = await client.post("/api/assets/upload", files={"file": ("auth.png", io.BytesIO(b"x"), "application/octet-stream")})
        asset_id = upload.json()["id"]

        resp = await unauthed_client.get(f"/api/assets/{asset_id}/preview")
        assert resp.status_code in (401, 303)


@pytest.mark.asyncio
class TestImageDuration:
    async def test_image_with_duration_shows_dash(self, client, db_session):
        """An image asset with duration_seconds set (e.g. HEIC) should show
        '—' in the assets page, not '00:00:00'."""
        from cms.models.asset import Asset, AssetType

        asset = Asset(
            filename="photo.jpg", original_filename="photo.heic",
            asset_type=AssetType.IMAGE, size_bytes=50000, checksum="img123",
            duration_seconds=0.04,  # ffprobe artefact from HEIC
        )
        db_session.add(asset)
        await db_session.commit()

        resp = await client.get("/assets")
        assert resp.status_code == 200
        html = resp.text
        assert "00:00:00" not in html
