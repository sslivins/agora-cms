"""Phase 1 write-path tests for hybrid tag-timeline slideshows.

The hybrid redesign folded the retired 1:1 ``slideshow_tag_rules`` table
into ordinary ``slideshow_slides`` rows of ``kind='tag'``.  A tag block is
a dynamic slide that expands in-place at resolve time to the current
membership of its tag.  These tests pin the create/replace write path:

* create + replace a deck containing a ``kind='tag'`` block
* GET /slides surfaces tag_name / tag_order_by / member_count
* the 409 tag-unaware-replace guard protects existing tag blocks
* slideshow_anchor_at is stamped / preserved / cleared correctly
* duration denormalisation = member_count x duration_ms
* a missing tag id 404s
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from cms.models.asset import Asset, AssetType
from cms.models.slideshow_slide import SlideshowSlide
from cms.models.tag import AssetTag, Tag


async def _seed_image(db_session, *, filename="img.png", is_global=True, checksum="img-cs"):
    asset = Asset(
        filename=filename,
        asset_type=AssetType.IMAGE,
        size_bytes=1234,
        checksum=checksum,
        is_global=is_global,
    )
    db_session.add(asset)
    await db_session.commit()
    await db_session.refresh(asset)
    return asset


async def _seed_tag(db_session, *, name="weekly-sale"):
    tag = Tag(name=name)
    db_session.add(tag)
    await db_session.commit()
    await db_session.refresh(tag)
    return tag


async def _tag_asset(db_session, asset, tag):
    db_session.add(AssetTag(asset_id=asset.id, tag_id=tag.id))
    await db_session.commit()


@pytest.mark.asyncio
class TestSlideshowTagBlockCreate:
    async def test_create_with_tag_block_persists_tag_slide(self, client, db_session):
        tag = await _seed_tag(db_session)
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "tagged",
                "slides": [
                    {"kind": "tag", "tag_id": str(tag.id), "duration_ms": 1000},
                ],
            },
        )
        assert create.status_code == 201, create.text
        sid = create.json()["id"]

        rows = (
            await db_session.execute(
                select(SlideshowSlide).where(
                    SlideshowSlide.slideshow_asset_id == uuid.UUID(sid)
                )
            )
        ).scalars().all()
        assert len(rows) == 1
        row = rows[0]
        assert row.kind == "tag"
        assert row.tag_id == tag.id
        assert row.source_asset_id is None
        assert row.tag_order_by == "tagged_at"

    async def test_create_with_unknown_tag_404s(self, client, db_session):
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "bad",
                "slides": [
                    {"kind": "tag", "tag_id": str(uuid.uuid4()), "duration_ms": 1000},
                ],
            },
        )
        assert create.status_code == 404, create.text
        assert "tag" in create.json()["detail"].lower()

    async def test_get_slides_surfaces_tag_metadata_and_member_count(
        self, client, db_session
    ):
        tag = await _seed_tag(db_session, name="promo")
        m1 = await _seed_image(db_session, filename="m1.png")
        m2 = await _seed_image(db_session, filename="m2.png")
        await _tag_asset(db_session, m1, tag)
        await _tag_asset(db_session, m2, tag)

        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "promo-show",
                "slides": [
                    {"kind": "tag", "tag_id": str(tag.id), "duration_ms": 1000},
                ],
            },
        )
        assert create.status_code == 201, create.text
        sid = create.json()["id"]

        body = (await client.get(f"/api/assets/{sid}/slides")).json()
        assert len(body["slides"]) == 1
        s = body["slides"][0]
        assert s["kind"] == "tag"
        assert s["tag_id"] == str(tag.id)
        assert s["tag_name"] == "promo"
        assert s["tag_order_by"] == "tagged_at"
        assert s["member_count"] == 2

    async def test_create_tag_block_duration_is_member_count_times_duration(
        self, client, db_session
    ):
        tag = await _seed_tag(db_session, name="dur")
        for i in range(3):
            m = await _seed_image(db_session, filename=f"d{i}.png")
            await _tag_asset(db_session, m, tag)

        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "dur-show",
                "slides": [
                    {"kind": "tag", "tag_id": str(tag.id), "duration_ms": 2000},
                ],
            },
        )
        assert create.status_code == 201, create.text
        # 3 members x 2000 ms = 6.0 s
        ss = await db_session.get(Asset, uuid.UUID(create.json()["id"]))
        await db_session.refresh(ss)
        assert ss.duration_seconds == pytest.approx(6.0)


@pytest.mark.asyncio
class TestSlideshowTagBlockReplace:
    async def _mint_empty(self, client):
        create = await client.post(
            "/api/assets/slideshow", json={"name": "deck", "slides": []}
        )
        assert create.status_code == 201, create.text
        return create.json()["id"]

    async def test_replace_mixed_asset_and_tag_block(self, client, db_session):
        tag = await _seed_tag(db_session, name="mix")
        img = await _seed_image(db_session, filename="static.png")
        sid = await self._mint_empty(client)

        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={
                "slides": [
                    {"kind": "asset", "source_asset_id": str(img.id), "duration_ms": 1000},
                    {"kind": "tag", "tag_id": str(tag.id), "duration_ms": 1000},
                ]
            },
        )
        assert put.status_code == 200, put.text
        assert put.json()["slide_count"] == 2

        rows = (
            await db_session.execute(
                select(SlideshowSlide)
                .where(SlideshowSlide.slideshow_asset_id == uuid.UUID(sid))
                .order_by(SlideshowSlide.position.asc())
            )
        ).scalars().all()
        assert [r.kind for r in rows] == ["asset", "tag"]
        assert rows[0].source_asset_id == img.id
        assert rows[1].tag_id == tag.id

    async def test_tag_unaware_replace_rejected_409(self, client, db_session):
        # Seed a deck with a tag block, then PUT a non-empty payload that
        # carries ZERO 'kind' keys (an old tag-unaware client). The guard
        # must 409 rather than silently dropping the tag block.
        tag = await _seed_tag(db_session, name="guard")
        img = await _seed_image(db_session, filename="g.png")
        sid = await self._mint_empty(client)
        ok = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{"kind": "tag", "tag_id": str(tag.id), "duration_ms": 1000}]},
        )
        assert ok.status_code == 200, ok.text

        clobber = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}]},
        )
        assert clobber.status_code == 409, clobber.text
        assert "tag" in clobber.json()["detail"].lower()

        # The tag block survived the rejected write.
        rows = (
            await db_session.execute(
                select(SlideshowSlide).where(
                    SlideshowSlide.slideshow_asset_id == uuid.UUID(sid)
                )
            )
        ).scalars().all()
        assert [r.kind for r in rows] == ["tag"]

    async def test_tag_unaware_replace_allowed_when_no_existing_tag(
        self, client, db_session
    ):
        # The guard only fires when the existing deck has a tag block. A
        # pure-asset deck must still accept a kind-less (legacy) payload.
        img = await _seed_image(db_session, filename="legacy.png")
        sid = await self._mint_empty(client)
        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}]},
        )
        assert put.status_code == 200, put.text
        assert put.json()["slide_count"] == 1


@pytest.mark.asyncio
class TestSlideshowAnchor:
    async def _mint_empty(self, client):
        create = await client.post(
            "/api/assets/slideshow", json={"name": "anchordeck", "slides": []}
        )
        assert create.status_code == 201, create.text
        return create.json()["id"]

    async def test_anchor_stamped_when_tag_block_added(self, client, db_session):
        tag = await _seed_tag(db_session, name="anchor")
        sid = await self._mint_empty(client)
        ss = await db_session.get(Asset, uuid.UUID(sid))
        await db_session.refresh(ss)
        assert ss.slideshow_anchor_at is None

        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{"kind": "tag", "tag_id": str(tag.id), "duration_ms": 1000}]},
        )
        assert put.status_code == 200, put.text
        db_session.expire(ss)
        ss = await db_session.get(Asset, uuid.UUID(sid))
        assert ss.slideshow_anchor_at is not None

    async def test_anchor_preserved_across_tag_edits(self, client, db_session):
        tag = await _seed_tag(db_session, name="preserve")
        sid = await self._mint_empty(client)
        await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{"kind": "tag", "tag_id": str(tag.id), "duration_ms": 1000}]},
        )
        ss = await db_session.get(Asset, uuid.UUID(sid))
        await db_session.refresh(ss)
        first_anchor = ss.slideshow_anchor_at
        assert first_anchor is not None

        # Re-PUT another tag-bearing deck; the anchor must NOT be re-stamped.
        await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{"kind": "tag", "tag_id": str(tag.id), "duration_ms": 2000}]},
        )
        db_session.expire(ss)
        ss = await db_session.get(Asset, uuid.UUID(sid))
        assert ss.slideshow_anchor_at == first_anchor

    async def test_anchor_cleared_when_no_tag_blocks_remain(self, client, db_session):
        tag = await _seed_tag(db_session, name="clear")
        img = await _seed_image(db_session, filename="c.png")
        sid = await self._mint_empty(client)
        await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{"kind": "tag", "tag_id": str(tag.id), "duration_ms": 1000}]},
        )
        ss = await db_session.get(Asset, uuid.UUID(sid))
        await db_session.refresh(ss)
        assert ss.slideshow_anchor_at is not None

        # Replace with an all-asset deck; anchor clears to NULL.
        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{"kind": "asset", "source_asset_id": str(img.id), "duration_ms": 1000}]},
        )
        assert put.status_code == 200, put.text
        db_session.expire(ss)
        ss = await db_session.get(Asset, uuid.UUID(sid))
        assert ss.slideshow_anchor_at is None
