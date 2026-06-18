"""Tests for the slideshow tag-rule API (agora-cms#806, Option B).

Covers GET / PUT / DELETE ``/api/assets/{id}/tag-rule``: the endpoints
that flip a slideshow into *tag mode* (deck resolved live from assets
carrying a tag) and back to manual mode.

Asserted contract:

* PUT creates a rule, stamps ``anchor_at`` to ~now, hardcodes
  ``order_by="tagged_at"``, and reports the live ``member_count``.
* PUT on an existing rule edits the defaults but PRESERVES ``anchor_at``
  (the no-restart guarantee — the on-screen slide must not jump).
* GET returns the rule (404 when manual mode).
* DELETE removes the rule (404 when none); manual slides survive.
* Validation: 404 for an unknown tag, 400 for a non-slideshow asset,
  422 for a bad default field.
* Owner/admin gating mirrors ``replace_slideshow_slides``.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from cms.models.asset import Asset, AssetType
from cms.models.audit_log import AuditLog
from cms.models.slideshow_slide import SlideshowSlide
from cms.models.slideshow_tag_rule import SlideshowTagRule
from cms.models.tag import AssetTag, Tag


# ── Seed helpers ──


async def _seed_image(db_session, *, filename="img.png", is_global=True):
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


async def _seed_tag(db_session, name="promos"):
    tag = Tag(name=name)
    db_session.add(tag)
    await db_session.commit()
    await db_session.refresh(tag)
    return tag


async def _tag_asset(db_session, asset, tag):
    db_session.add(AssetTag(asset_id=asset.id, tag_id=tag.id))
    await db_session.commit()


async def _create_slideshow(client, name="tagdeck"):
    create = await client.post(
        "/api/assets/slideshow", json={"name": name, "slides": []}
    )
    assert create.status_code == 201, create.text
    return create.json()["id"]


@pytest.mark.asyncio
class TestTagRuleApi:

    async def test_get_404_when_no_rule(self, client, db_session):
        sid = await _create_slideshow(client)
        resp = await client.get(f"/api/assets/{sid}/tag-rule")
        assert resp.status_code == 404

    async def test_put_creates_rule_stamps_anchor_and_defaults(
        self, client, db_session
    ):
        sid = await _create_slideshow(client)
        tag = await _seed_tag(db_session)
        before = datetime.now(timezone.utc)
        resp = await client.put(
            f"/api/assets/{sid}/tag-rule", json={"tag_id": str(tag.id)}
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["slideshow_asset_id"] == sid
        assert body["tag_id"] == str(tag.id)
        assert body["tag_name"] == "promos"
        assert body["order_by"] == "tagged_at"
        # Schema defaults applied.
        assert body["default_duration_ms"] == 8000
        assert body["default_transition"] == "cut"
        assert body["default_fit"] == "cover"
        # anchor_at stamped to ~now on create.
        anchor = datetime.fromisoformat(body["anchor_at"])
        if anchor.tzinfo is None:
            anchor = anchor.replace(tzinfo=timezone.utc)
        assert (datetime.now(timezone.utc) - anchor).total_seconds() < 60
        assert anchor >= before.replace(microsecond=0)

        rule = (
            await db_session.execute(
                select(SlideshowTagRule).where(
                    SlideshowTagRule.slideshow_asset_id == uuid.UUID(sid)
                )
            )
        ).scalar_one()
        assert rule.anchor_at is not None

    async def test_member_count_reflects_tagged_assets(self, client, db_session):
        sid = await _create_slideshow(client)
        tag = await _seed_tag(db_session)
        img1 = await _seed_image(db_session, filename="m1.png")
        img2 = await _seed_image(db_session, filename="m2.png")
        await _tag_asset(db_session, img1, tag)
        await _tag_asset(db_session, img2, tag)
        resp = await client.put(
            f"/api/assets/{sid}/tag-rule", json={"tag_id": str(tag.id)}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["member_count"] == 2

    async def test_get_returns_existing_rule(self, client, db_session):
        sid = await _create_slideshow(client)
        tag = await _seed_tag(db_session)
        await client.put(f"/api/assets/{sid}/tag-rule", json={"tag_id": str(tag.id)})
        resp = await client.get(f"/api/assets/{sid}/tag-rule")
        assert resp.status_code == 200, resp.text
        assert resp.json()["tag_id"] == str(tag.id)

    async def test_put_edit_preserves_anchor_at(self, client, db_session):
        sid = await _create_slideshow(client)
        tag = await _seed_tag(db_session)
        first = await client.put(
            f"/api/assets/{sid}/tag-rule", json={"tag_id": str(tag.id)}
        )
        anchor1 = first.json()["anchor_at"]

        # Edit the defaults — anchor_at must NOT move (no-restart guarantee).
        second = await client.put(
            f"/api/assets/{sid}/tag-rule",
            json={
                "tag_id": str(tag.id),
                "default_duration_ms": 12000,
                "default_transition": "fade",
            },
        )
        assert second.status_code == 200, second.text
        body = second.json()
        assert body["anchor_at"] == anchor1
        assert body["default_duration_ms"] == 12000
        assert body["default_transition"] == "fade"

    async def test_put_can_switch_tag(self, client, db_session):
        sid = await _create_slideshow(client)
        tag_a = await _seed_tag(db_session, name="a")
        tag_b = await _seed_tag(db_session, name="b")
        await client.put(f"/api/assets/{sid}/tag-rule", json={"tag_id": str(tag_a.id)})
        resp = await client.put(
            f"/api/assets/{sid}/tag-rule", json={"tag_id": str(tag_b.id)}
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["tag_id"] == str(tag_b.id)
        # Still exactly one rule row (upsert, not insert).
        rows = (
            await db_session.execute(
                select(SlideshowTagRule).where(
                    SlideshowTagRule.slideshow_asset_id == uuid.UUID(sid)
                )
            )
        ).scalars().all()
        assert len(rows) == 1

    async def test_put_unknown_tag_404(self, client, db_session):
        sid = await _create_slideshow(client)
        resp = await client.put(
            f"/api/assets/{sid}/tag-rule", json={"tag_id": str(uuid.uuid4())}
        )
        assert resp.status_code == 404
        assert "tag" in resp.json()["detail"].lower()

    async def test_put_non_slideshow_400(self, client, db_session):
        img = await _seed_image(db_session)
        tag = await _seed_tag(db_session)
        resp = await client.put(
            f"/api/assets/{img.id}/tag-rule", json={"tag_id": str(tag.id)}
        )
        assert resp.status_code == 400
        assert "slideshow" in resp.json()["detail"].lower()

    async def test_put_invalid_default_422(self, client, db_session):
        sid = await _create_slideshow(client)
        tag = await _seed_tag(db_session)
        resp = await client.put(
            f"/api/assets/{sid}/tag-rule",
            json={"tag_id": str(tag.id), "default_transition": "not-a-transition"},
        )
        assert resp.status_code == 422

    async def test_delete_removes_rule(self, client, db_session):
        sid = await _create_slideshow(client)
        tag = await _seed_tag(db_session)
        await client.put(f"/api/assets/{sid}/tag-rule", json={"tag_id": str(tag.id)})
        resp = await client.delete(f"/api/assets/{sid}/tag-rule")
        assert resp.status_code == 200, resp.text
        assert resp.json()["tag_rule"] is None
        # Rule gone.
        assert (
            await client.get(f"/api/assets/{sid}/tag-rule")
        ).status_code == 404

    async def test_delete_404_when_no_rule(self, client, db_session):
        sid = await _create_slideshow(client)
        resp = await client.delete(f"/api/assets/{sid}/tag-rule")
        assert resp.status_code == 404

    async def test_delete_leaves_manual_slides_intact(self, client, db_session):
        # Tag mode coexists with (and overrides) a manual deck; deleting the
        # rule must revert to that manual deck, so manual rows survive.
        img = await _seed_image(db_session, filename="manual.png")
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "withmanual",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 3000}],
            },
        )
        assert create.status_code == 201, create.text
        sid = create.json()["id"]
        tag = await _seed_tag(db_session)
        await client.put(f"/api/assets/{sid}/tag-rule", json={"tag_id": str(tag.id)})
        await client.delete(f"/api/assets/{sid}/tag-rule")

        rows = (
            await db_session.execute(
                select(SlideshowSlide).where(
                    SlideshowSlide.slideshow_asset_id == uuid.UUID(sid)
                )
            )
        ).scalars().all()
        assert len(rows) == 1

    async def test_put_and_delete_write_audit_log(self, client, db_session):
        sid = await _create_slideshow(client)
        tag = await _seed_tag(db_session)
        await client.put(f"/api/assets/{sid}/tag-rule", json={"tag_id": str(tag.id)})
        await client.delete(f"/api/assets/{sid}/tag-rule")
        actions = (
            await db_session.execute(
                select(AuditLog.action).where(AuditLog.resource_id == sid)
            )
        ).scalars().all()
        assert "asset.set_tag_rule" in actions
        assert "asset.delete_tag_rule" in actions
