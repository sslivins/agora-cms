"""Tests for the bare-slideshow live preview endpoint.

``GET /composed/slideshow/{asset_id}/preview`` wraps a SLIDESHOW asset in
an ephemeral single full-bleed ``media`` widget Layout and reuses the
composed render pipeline so a slideshow previews with the same
CSS-transition cycling the device uses. No persisted ComposedSlide is
involved.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from cms.models.asset import Asset, AssetType
from cms.models.slideshow_slide import SlideshowSlide

# A 1x1 PNG (valid header is enough — we only inline bytes).
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


def _storage_dir(tmp_path):
    # Mirrors the conftest ``app`` fixture: asset_storage_path = tmp_path/"assets".
    d = tmp_path / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.mark.asyncio
class TestSlideshowPreview:
    async def _make_image(self, db_session, storage, name, *, is_global=True):
        (storage / name).write_bytes(_PNG_BYTES)
        img = Asset(
            filename=name,
            asset_type=AssetType.IMAGE,
            size_bytes=len(_PNG_BYTES),
            checksum="x",
            is_global=is_global,
        )
        db_session.add(img)
        await db_session.flush()
        return img

    async def _make_slideshow(self, db_session, members, *, is_global=True):
        ss = Asset(
            filename=f"show-{uuid.uuid4()}.slideshow",
            asset_type=AssetType.SLIDESHOW,
            size_bytes=0,
            checksum="",
            is_global=is_global,
        )
        db_session.add(ss)
        await db_session.flush()
        for idx, src in enumerate(members):
            db_session.add(
                SlideshowSlide(
                    slideshow_asset_id=ss.id,
                    source_asset_id=src.id,
                    position=idx,
                    duration_ms=4000,
                    play_to_end=False,
                    transition="fade",
                    transition_ms=600,
                )
            )
        await db_session.flush()
        await db_session.commit()
        return ss

    async def test_404_when_asset_missing(self, client):
        resp = await client.get(f"/composed/slideshow/{uuid.uuid4()}/preview")
        assert resp.status_code == 404

    async def test_404_when_wrong_asset_type(self, client, db_session):
        asset = Asset(
            filename="not_a_slideshow.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=100,
            checksum="abc",
        )
        db_session.add(asset)
        await db_session.commit()

        resp = await client.get(f"/composed/slideshow/{asset.id}/preview")
        assert resp.status_code == 404

    async def test_404_when_slideshow_deleted(self, client, db_session, tmp_path):
        storage = _storage_dir(tmp_path)
        img = await self._make_image(db_session, storage, "del.png")
        ss = await self._make_slideshow(db_session, [img])
        ss.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()

        resp = await client.get(f"/composed/slideshow/{ss.id}/preview")
        assert resp.status_code == 404

    async def test_happy_path_inlines_each_member(
        self, client, db_session, tmp_path
    ):
        storage = _storage_dir(tmp_path)
        img1 = await self._make_image(db_session, storage, "a.png")
        img2 = await self._make_image(db_session, storage, "b.png")
        ss = await self._make_slideshow(db_session, [img1, img2])

        resp = await client.get(f"/composed/slideshow/{ss.id}/preview")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "<html" in body.lower()
        # Both members inlined as data URIs; ported CSS-transition markup present.
        assert body.count("data:image/png;base64,") == 2
        assert "cw-ss-slide" in body
        # No device-local sibling path should leak into a preview.
        assert "/assets/videos/" not in body

    async def test_empty_slideshow_is_422(self, client, db_session, tmp_path):
        _storage_dir(tmp_path)
        ss = await self._make_slideshow(db_session, [])

        resp = await client.get(f"/composed/slideshow/{ss.id}/preview")
        assert resp.status_code == 422, resp.text
        assert "has no slides" in resp.text

    async def test_non_media_member_is_422(self, client, db_session, tmp_path):
        _storage_dir(tmp_path)
        web = Asset(
            filename="page.url",
            asset_type=AssetType.WEBPAGE,
            size_bytes=0,
            checksum="",
            is_global=True,
        )
        db_session.add(web)
        await db_session.flush()
        ss = await self._make_slideshow(db_session, [web])

        resp = await client.get(f"/composed/slideshow/{ss.id}/preview")
        assert resp.status_code == 422, resp.text
        assert "can only cycle IMAGE and VIDEO" in resp.text

    async def test_csp_header_is_locked_down(self, client, db_session, tmp_path):
        storage = _storage_dir(tmp_path)
        img = await self._make_image(db_session, storage, "csp.png")
        ss = await self._make_slideshow(db_session, [img])

        resp = await client.get(f"/composed/slideshow/{ss.id}/preview")
        assert resp.status_code == 200, resp.text
        csp = resp.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'self'" in csp
        # A slideshow can't contain weather/rss/iframe widgets, so no
        # external connect/frame origins should ever leak in.
        assert "http:" not in csp
        assert "https:" not in csp
        assert resp.headers.get("x-content-type-options") == "nosniff"

    async def test_no_cache_header_set(self, client, db_session, tmp_path):
        storage = _storage_dir(tmp_path)
        img = await self._make_image(db_session, storage, "nc.png")
        ss = await self._make_slideshow(db_session, [img])

        resp = await client.get(f"/composed/slideshow/{ss.id}/preview")
        assert resp.status_code == 200, resp.text
        assert resp.headers.get("cache-control") == "no-store"

    async def test_unauthorized_user_cannot_preview(
        self, operator_client, client, db_session, tmp_path
    ):
        # A non-admin Operator with no group memberships can only see
        # global assets; a personal/un-shared slideshow must 403/404.
        storage = _storage_dir(tmp_path)
        img = await self._make_image(
            db_session, storage, "priv.png", is_global=False
        )
        ss = await self._make_slideshow(db_session, [img], is_global=False)

        resp = await operator_client.get(f"/composed/slideshow/{ss.id}/preview")
        assert resp.status_code in (403, 404), resp.text
        # Admin can still see it (sanity).
        ok = await client.get(f"/composed/slideshow/{ss.id}/preview")
        assert ok.status_code == 200, ok.text
