"""Tests for asset API endpoints."""

import io

import pytest


def _make_upload(filename: str, content: bytes = b"fakecontent"):
    return {"file": (filename, io.BytesIO(content), "application/octet-stream")}


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
