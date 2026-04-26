"""Smoke tests for slideshow builder UI routes (Commit 4)."""

from __future__ import annotations

import uuid

import pytest

from cms.models.asset import Asset, AssetType
from cms.models.slideshow_slide import SlideshowSlide
from cms.models.user import User


pytestmark = pytest.mark.asyncio


async def _seed_slideshow(db_session, *, name="My Show", is_global=True, slides=0):
    asset = Asset(
        filename=name,
        asset_type=AssetType.SLIDESHOW,
        size_bytes=0,
        checksum="v1",
        duration_seconds=10.0,
        is_global=is_global,
    )
    db_session.add(asset)
    await db_session.flush()
    if slides:
        # Need a real source asset to FK against
        src = Asset(
            filename=f"src-{uuid.uuid4().hex[:6]}.png",
            asset_type=AssetType.IMAGE,
            size_bytes=100,
            is_global=True,
        )
        db_session.add(src)
        await db_session.flush()
        for i in range(slides):
            db_session.add(SlideshowSlide(
                slideshow_asset_id=asset.id,
                source_asset_id=src.id,
                position=i,
                duration_ms=5000,
                play_to_end=False,
            ))
    await db_session.commit()
    return asset


class TestSlideshowBuilderRoutes:

    async def test_new_page_renders(self, client):
        resp = await client.get("/assets/new/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "New Slideshow" in body
        assert "ss-slides-table" in body
        assert "/api/assets/slideshow" in body  # JS POST endpoint baked in

    async def test_edit_page_renders_with_existing_slides(self, client, db_session):
        asset = await _seed_slideshow(db_session, name="Editable", slides=3)
        resp = await client.get(f"/assets/{asset.id}/slideshow")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "Edit Slideshow" in body
        assert "Editable" in body
        # Ensure the seeded slides are in the JSON island the page uses for state
        assert '"position": 0' in body or '"position":0' in body

    async def test_edit_page_404s_for_non_slideshow(self, client, db_session):
        # Image, not slideshow: should redirect away
        img = Asset(
            filename="not-a-show.png",
            asset_type=AssetType.IMAGE,
            size_bytes=10,
            is_global=True,
        )
        db_session.add(img)
        await db_session.commit()
        resp = await client.get(f"/assets/{img.id}/slideshow", follow_redirects=False)
        assert resp.status_code in (303, 307, 302)

    async def test_edit_page_redirects_for_unknown_id(self, client):
        bogus = uuid.uuid4()
        resp = await client.get(f"/assets/{bogus}/slideshow", follow_redirects=False)
        assert resp.status_code in (303, 307, 302)

    async def test_assets_page_shows_create_slideshow_link(self, client, db_session):
        resp = await client.get("/assets")
        assert resp.status_code == 200
        assert "/assets/new/slideshow" in resp.text
        assert "New Slideshow" in resp.text

    async def test_assets_page_shows_slide_count_badge(self, client, db_session):
        await _seed_slideshow(db_session, name="Count Show", slides=3)
        resp = await client.get("/assets")
        assert resp.status_code == 200
        # Badge text from _macros.html
        assert "3 slides" in resp.text
