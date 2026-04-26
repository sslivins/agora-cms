"""Tests for the slideshow virtual asset feature (Commit 1).

Covers:

* schema (FK cascade on slideshow delete; FK restrict on source delete;
  unique (slideshow, position))
* POST /api/assets/slideshow create + validation matrix
* GET / PUT /api/assets/{id}/slides round-trip
* ACL invariant (global + group-scoped)
* source-asset delete guard while slideshow references exist
* unshare / unmark-global guards on source assets
* audit log entries for create + replace
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cms.models.asset import Asset, AssetType
from cms.models.audit_log import AuditLog
from cms.models.device import DeviceGroup
from cms.models.group_asset import GroupAsset
from cms.models.slideshow_slide import SlideshowSlide
from cms.models.user import User


# ── Helpers ──


async def _seed_image(db_session, *, filename="img.png", is_global=False):
    asset = Asset(
        filename=filename,
        asset_type=AssetType.IMAGE,
        size_bytes=1234,
        checksum="img-cs",
        is_global=is_global,
    )
    db_session.add(asset)
    await db_session.commit()
    await db_session.refresh(asset)
    return asset


async def _seed_video(db_session, *, filename="vid.mp4", is_global=False, duration=12.0):
    asset = Asset(
        filename=filename,
        asset_type=AssetType.VIDEO,
        size_bytes=99999,
        checksum="vid-cs",
        is_global=is_global,
        duration_seconds=duration,
    )
    db_session.add(asset)
    await db_session.commit()
    await db_session.refresh(asset)
    return asset


async def _seed_webpage(db_session, *, filename="page", is_global=False):
    asset = Asset(
        filename=filename,
        asset_type=AssetType.WEBPAGE,
        size_bytes=0,
        checksum="",
        url="https://example.com/x",
        is_global=is_global,
    )
    db_session.add(asset)
    await db_session.commit()
    await db_session.refresh(asset)
    return asset


async def _seed_group(db_session, name="group-a"):
    g = DeviceGroup(name=name)
    db_session.add(g)
    await db_session.commit()
    await db_session.refresh(g)
    return g


async def _share(db_session, asset, group):
    db_session.add(GroupAsset(asset_id=asset.id, group_id=group.id))
    await db_session.commit()


# ── Schema ──


@pytest.mark.asyncio
class TestSlideshowSchema:

    async def test_cascade_on_slideshow_delete(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "cascade-test",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 5000}],
            },
        )
        assert resp.status_code == 201, resp.text
        slideshow_id = uuid.UUID(resp.json()["id"])

        # Hard-delete via session bypassing soft-delete path (we just want to
        # exercise the FK CASCADE, not the API's soft-delete guard).
        ss = await db_session.get(Asset, slideshow_id)
        await db_session.delete(ss)
        await db_session.commit()

        rows = (await db_session.execute(
            select(SlideshowSlide).where(
                SlideshowSlide.slideshow_asset_id == slideshow_id
            )
        )).scalars().all()
        assert rows == []

    async def test_restrict_on_source_delete(self, client, db_session):
        """At the database level, deleting a source asset that's still
        referenced by a slide row must fail (FK ON DELETE RESTRICT).  The
        API layer surfaces this as 409 — see TestSourceDeleteGuard."""
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "restrict-test",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 5000}],
            },
        )
        assert resp.status_code == 201, resp.text

        await db_session.delete(img)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    async def test_unique_slideshow_position(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "unique-pos",
                "slides": [
                    {"source_asset_id": str(img.id), "duration_ms": 1000},
                    {"source_asset_id": str(img.id), "duration_ms": 2000},
                ],
            },
        )
        assert resp.status_code == 201
        slideshow_id = uuid.UUID(resp.json()["id"])

        db_session.add(
            SlideshowSlide(
                slideshow_asset_id=slideshow_id,
                source_asset_id=img.id,
                position=0,  # duplicate
                duration_ms=999,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()


# ── Create endpoint ──


@pytest.mark.asyncio
class TestSlideshowCreate:

    async def test_create_happy_path(self, client, db_session):
        img = await _seed_image(db_session, filename="a.png", is_global=True)
        vid = await _seed_video(
            db_session, filename="b.mp4", is_global=True, duration=8.0
        )
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "Hello slideshow",
                "slides": [
                    {"source_asset_id": str(img.id), "duration_ms": 5000},
                    {
                        "source_asset_id": str(vid.id),
                        "duration_ms": 1000,
                        "play_to_end": True,
                    },
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["asset_type"] == "slideshow"
        assert body["filename"] == "Hello slideshow"
        # play_to_end on video uses source duration (8s), not 1s configured
        assert body["duration_seconds"] == pytest.approx(5.0 + 8.0)
        # is_global=True since admin + no groups
        assert body["is_global"] is True

    async def test_rejects_empty_slides(self, client, db_session):
        resp = await client.post(
            "/api/assets/slideshow",
            json={"name": "x", "slides": []},
        )
        assert resp.status_code == 400
        assert "at least one" in resp.json()["detail"].lower()

    async def test_rejects_too_many_slides(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        slides = [
            {"source_asset_id": str(img.id), "duration_ms": 1000} for _ in range(51)
        ]
        resp = await client.post(
            "/api/assets/slideshow", json={"name": "x", "slides": slides}
        )
        assert resp.status_code == 400
        assert "50" in resp.json()["detail"]

    async def test_rejects_missing_name(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "  ",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 400

    async def test_rejects_missing_source(self, client, db_session):
        ghost = uuid.uuid4()
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [{"source_asset_id": str(ghost), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 404

    async def test_rejects_webpage_source(self, client, db_session):
        page = await _seed_webpage(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [{"source_asset_id": str(page.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 400
        assert "image and video" in resp.json()["detail"].lower()

    async def test_rejects_play_to_end_on_image(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [
                    {
                        "source_asset_id": str(img.id),
                        "duration_ms": 1000,
                        "play_to_end": True,
                    }
                ],
            },
        )
        assert resp.status_code == 400
        assert "play_to_end" in resp.json()["detail"]

    async def test_rejects_duration_too_small(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 100}],
            },
        )
        assert resp.status_code == 400  # caught + re-raised as 400 by endpoint

    async def test_rejects_duration_too_large(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [
                    {"source_asset_id": str(img.id), "duration_ms": 60 * 60 * 1000 + 1}
                ],
            },
        )
        assert resp.status_code == 400


# ── ACL invariant ──


@pytest.mark.asyncio
class TestSlideshowACL:

    async def test_global_slideshow_requires_global_sources(
        self, client, db_session
    ):
        # Source NOT global; admin no-group create => slideshow becomes global.
        img = await _seed_image(db_session, is_global=False)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 400
        assert "global" in resp.json()["detail"].lower()

    async def test_group_slideshow_requires_source_in_group(
        self, client, db_session
    ):
        g = await _seed_group(db_session, "g1")
        img = await _seed_image(db_session, is_global=False)
        # Source not shared with g.
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "group_ids": [str(g.id)],
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 400

    async def test_group_slideshow_succeeds_when_source_shared(
        self, client, db_session
    ):
        g = await _seed_group(db_session, "g2")
        img = await _seed_image(db_session, is_global=False)
        await _share(db_session, img, g)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "group_ids": [str(g.id)],
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["is_global"] is False


# ── GET / PUT slides ──


@pytest.mark.asyncio
class TestSlideshowSlidesEndpoints:

    async def test_get_slides_returns_ordered_with_source_metadata(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="aa.png", is_global=True)
        vid = await _seed_video(
            db_session, filename="bb.mp4", is_global=True, duration=4.0
        )
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "g",
                "slides": [
                    {"source_asset_id": str(vid.id), "duration_ms": 1000},
                    {"source_asset_id": str(img.id), "duration_ms": 2000},
                ],
            },
        )
        assert create.status_code == 201, create.text
        sid = create.json()["id"]
        resp = await client.get(f"/api/assets/{sid}/slides")
        assert resp.status_code == 200
        body = resp.json()
        assert [s["position"] for s in body["slides"]] == [0, 1]
        assert body["slides"][0]["source_filename"] == "bb.mp4"
        assert body["slides"][0]["source_asset_type"] == "video"
        assert body["slides"][1]["source_filename"] == "aa.png"

    async def test_get_slides_404_for_non_slideshow(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.get(f"/api/assets/{img.id}/slides")
        assert resp.status_code == 400
        assert "slideshow" in resp.json()["detail"].lower()

    async def test_replace_slides_updates_duration_and_rows(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="r1.png", is_global=True)
        img2 = await _seed_image(db_session, filename="r2.png", is_global=True)
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "rep",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert create.status_code == 201
        sid = create.json()["id"]
        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={
                "slides": [
                    {"source_asset_id": str(img2.id), "duration_ms": 4000},
                    {"source_asset_id": str(img.id), "duration_ms": 1500},
                ]
            },
        )
        assert put.status_code == 200, put.text
        assert put.json()["slide_count"] == 2
        assert put.json()["duration_seconds"] == pytest.approx(5.5)

        # And the asset row reflects it
        ss = await db_session.get(Asset, uuid.UUID(sid))
        await db_session.refresh(ss)
        assert ss.duration_seconds == pytest.approx(5.5)


# ── Source-delete guard ──


@pytest.mark.asyncio
class TestSourceDeleteGuard:

    async def test_blocks_delete_when_referenced_by_active_slideshow(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="locked.png", is_global=True)
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "blockit",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert create.status_code == 201
        resp = await client.delete(f"/api/assets/{img.id}")
        assert resp.status_code == 409
        assert "blockit" in resp.json()["detail"]
        assert "active slideshow" in resp.json()["detail"].lower()

    async def test_blocks_delete_when_referenced_by_soft_deleted_slideshow(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="locked2.png", is_global=True)
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "softdeleted",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        sid = uuid.UUID(create.json()["id"])
        # Soft-delete the slideshow (via API, which sets deleted_at)
        del_ss = await client.delete(f"/api/assets/{sid}")
        assert del_ss.status_code == 200, del_ss.text
        # Source delete should still be blocked while the soft-deleted
        # slideshow exists (FK is RESTRICT — reaper will eventually clear).
        resp = await client.delete(f"/api/assets/{img.id}")
        assert resp.status_code == 409
        assert "soft-deleted" in resp.json()["detail"].lower()


# ── Source-side ACL guards ──


@pytest.mark.asyncio
class TestSourceSideACLGuards:

    async def test_unshare_blocked_when_slideshow_in_same_group(
        self, client, db_session
    ):
        g = await _seed_group(db_session, "g3")
        img = await _seed_image(db_session, filename="shared.png", is_global=False)
        await _share(db_session, img, g)
        ss_resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "share-blocker",
                "group_ids": [str(g.id)],
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert ss_resp.status_code == 201, ss_resp.text
        unshare = await client.delete(
            f"/api/assets/{img.id}/share?group_id={g.id}"
        )
        assert unshare.status_code == 409
        assert "share-blocker" in unshare.json()["detail"]

    async def test_unmark_global_blocked_when_global_slideshow_uses_it(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="g-src.png", is_global=True)
        ss_resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "global-blocker",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert ss_resp.status_code == 201
        # Toggling off global on the source must be refused
        toggle = await client.post(f"/api/assets/{img.id}/global")
        assert toggle.status_code == 409
        assert "global-blocker" in toggle.json()["detail"]
        # Source is still global
        await db_session.refresh(img)
        assert img.is_global is True


# ── Audit logging ──


@pytest.mark.asyncio
class TestSlideshowAudit:

    async def test_create_writes_audit_log(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "audited",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 201
        sid = resp.json()["id"]
        rows = (await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "asset.create_slideshow",
                AuditLog.resource_id == sid,
            )
        )).scalars().all()
        assert len(rows) == 1
        assert "audited" in rows[0].description

    async def test_replace_writes_audit_log(self, client, db_session):
        img = await _seed_image(db_session, filename="x1.png", is_global=True)
        img2 = await _seed_image(db_session, filename="x2.png", is_global=True)
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "replaceaudit",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        sid = create.json()["id"]
        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={
                "slides": [
                    {"source_asset_id": str(img2.id), "duration_ms": 1500}
                ]
            },
        )
        assert put.status_code == 200
        rows = (await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "asset.replace_slides",
                AuditLog.resource_id == sid,
            )
        )).scalars().all()
        assert len(rows) == 1
