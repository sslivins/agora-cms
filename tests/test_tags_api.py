"""Tests for the Tag CRUD API and the asset bulk add_tag / remove_tag actions."""

from __future__ import annotations

import pytest


async def _seed_assets(db_session, n: int = 3):
    from cms.models.asset import Asset, AssetType

    created = []
    for i in range(n):
        a = Asset(
            filename=f"asset-{i}.png",
            original_filename=f"asset-{i}.png",
            asset_type=AssetType.IMAGE,
            size_bytes=1000,
            checksum=f"chk{i}",
        )
        db_session.add(a)
        created.append(a)
    await db_session.commit()
    for a in created:
        await db_session.refresh(a)
    return created


async def _create_tag(client, name: str, color: str | None = None) -> dict:
    body = {"name": name}
    if color is not None:
        body["color"] = color
    resp = await client.post("/api/tags", json=body)
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
class TestTagCRUD:
    async def test_create_and_list(self, client):
        t = await _create_tag(client, "Promo")  # auto-lower-cased server-side
        assert t["name"] == "promo"
        assert t["color"] == "#737373"  # default

        listing = (await client.get("/api/tags")).json()
        assert any(x["id"] == t["id"] for x in listing)

    async def test_create_strips_whitespace_and_lowercases(self, client):
        t = await _create_tag(client, "  HoLiDay  ")
        assert t["name"] == "holiday"

    async def test_create_rejects_duplicate_name_case_insensitive(self, client):
        await _create_tag(client, "draft")
        resp = await client.post("/api/tags", json={"name": "DRAFT"})
        assert resp.status_code == 400
        assert "already exists" in resp.json()["detail"]

    async def test_create_rejects_blank(self, client):
        resp = await client.post("/api/tags", json={"name": "   "})
        assert resp.status_code == 422

    async def test_create_validates_color(self, client):
        resp = await client.post(
            "/api/tags", json={"name": "tag1", "color": "blue"}
        )
        assert resp.status_code == 422

    async def test_patch_rename_and_recolor(self, client):
        t = await _create_tag(client, "old", color="#abc")
        resp = await client.patch(
            f"/api/tags/{t['id']}",
            json={"name": "New Name", "color": "#112233"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "new name"
        assert body["color"] == "#112233"

    async def test_patch_rejects_rename_to_duplicate(self, client):
        a = await _create_tag(client, "alpha")
        b = await _create_tag(client, "beta")
        resp = await client.patch(f"/api/tags/{b['id']}", json={"name": "ALPHA"})
        assert resp.status_code == 400
        # Renaming to its own name is a no-op (not a duplicate).
        resp = await client.patch(f"/api/tags/{a['id']}", json={"name": "alpha"})
        assert resp.status_code == 200

    async def test_delete_cascades_through_junction(self, client, db_session):
        import uuid as _uuid
        from sqlalchemy import select
        from cms.models.tag import AssetTag

        assets = await _seed_assets(db_session, n=2)
        t = await _create_tag(client, "to-delete")
        tid = _uuid.UUID(t["id"])
        # Attach tag via bulk
        resp = await client.post(
            "/api/assets/bulk",
            json={
                "asset_ids": [str(a.id) for a in assets],
                "action": "add_tag",
                "tag_id": t["id"],
            },
        )
        assert resp.status_code == 200

        # Confirm junction populated.
        rows = (
            await db_session.execute(
                select(AssetTag).where(AssetTag.tag_id == tid)
            )
        ).scalars().all()
        assert len(rows) == 2

        resp = await client.delete(f"/api/tags/{t['id']}")
        assert resp.status_code == 204

        rows = (
            await db_session.execute(
                select(AssetTag).where(AssetTag.tag_id == tid)
            )
        ).scalars().all()
        assert rows == []

    async def test_list_includes_asset_count(self, client, db_session):
        assets = await _seed_assets(db_session, n=3)
        t = await _create_tag(client, "counted")
        await client.post(
            "/api/assets/bulk",
            json={
                "asset_ids": [str(a.id) for a in assets[:2]],
                "action": "add_tag",
                "tag_id": t["id"],
            },
        )
        listing = (await client.get("/api/tags")).json()
        row = next(x for x in listing if x["id"] == t["id"])
        assert row["asset_count"] == 2


@pytest.mark.asyncio
class TestAssetBulkTagActions:
    async def test_add_tag_requires_tag_id(self, client, db_session):
        assets = await _seed_assets(db_session, n=1)
        resp = await client.post(
            "/api/assets/bulk",
            json={"asset_ids": [str(assets[0].id)], "action": "add_tag"},
        )
        assert resp.status_code == 400
        assert "tag_id" in resp.json()["detail"]

    async def test_add_tag_404_when_tag_missing(self, client, db_session):
        assets = await _seed_assets(db_session, n=1)
        resp = await client.post(
            "/api/assets/bulk",
            json={
                "asset_ids": [str(assets[0].id)],
                "action": "add_tag",
                "tag_id": "00000000-0000-0000-0000-000000000000",
            },
        )
        assert resp.status_code == 404

    async def test_add_tag_idempotent(self, client, db_session):
        import uuid as _uuid
        from sqlalchemy import select
        from cms.models.tag import AssetTag

        assets = await _seed_assets(db_session, n=1)
        t = await _create_tag(client, "x")
        aid = str(assets[0].id)
        for _ in range(3):
            resp = await client.post(
                "/api/assets/bulk",
                json={"asset_ids": [aid], "action": "add_tag", "tag_id": t["id"]},
            )
            assert resp.status_code == 200
            assert resp.json()["succeeded"] == [aid]

        rows = (
            await db_session.execute(
                select(AssetTag).where(AssetTag.asset_id == assets[0].id)
            )
        ).scalars().all()
        assert len(rows) == 1

    async def test_remove_tag_succeeds_even_if_not_applied(self, client, db_session):
        assets = await _seed_assets(db_session, n=1)
        t = await _create_tag(client, "ghost")
        resp = await client.post(
            "/api/assets/bulk",
            json={
                "asset_ids": [str(assets[0].id)],
                "action": "remove_tag",
                "tag_id": t["id"],
            },
        )
        assert resp.status_code == 200
        assert resp.json()["succeeded"] == [str(assets[0].id)]

    async def test_add_tag_partial_failure(self, client, db_session):
        assets = await _seed_assets(db_session, n=2)
        t = await _create_tag(client, "p")
        ids = [str(a.id) for a in assets] + ["00000000-0000-0000-0000-000000000000"]
        resp = await client.post(
            "/api/assets/bulk",
            json={"asset_ids": ids, "action": "add_tag", "tag_id": t["id"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert sorted(data["succeeded"]) == sorted(str(a.id) for a in assets)
        assert len(data["failed"]) == 1
        assert data["failed"][0]["status"] == 404


@pytest.mark.asyncio
class TestTagMembersEndpoint:
    """GET /api/tags/{tag_id}/members — the slideshow-builder tag preview."""

    async def _tag_assets(self, db_session, tag_id, assets, base_ts):
        """Attach assets to a tag with strictly increasing tagged-at order."""
        import datetime as _dt

        from cms.models.tag import AssetTag

        for offset, a in enumerate(assets):
            db_session.add(
                AssetTag(
                    asset_id=a.id,
                    tag_id=tag_id,
                    created_at=base_ts + _dt.timedelta(seconds=offset),
                )
            )
        await db_session.commit()

    async def test_404_when_tag_missing(self, client):
        resp = await client.get(
            "/api/tags/00000000-0000-0000-0000-000000000000/members"
        )
        assert resp.status_code == 404

    async def test_returns_members_in_tagged_at_order(self, client, db_session):
        import datetime as _dt
        import uuid as _uuid

        assets = await _seed_assets(db_session, n=3)
        t = await _create_tag(client, "ordered")
        tid = _uuid.UUID(t["id"])
        # Attach in reverse asset order so insertion order != id order; the
        # endpoint must echo tagged-at (created_at) ascending.
        expected = list(reversed(assets))
        await self._tag_assets(
            db_session, tid, expected, _dt.datetime(2026, 1, 1, 12, 0, 0)
        )

        resp = await client.get(f"/api/tags/{t['id']}/members")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["tag_id"] == t["id"]
        assert data["total"] == 3
        assert [m["id"] for m in data["members"]] == [str(a.id) for a in expected]
        # Shape: every tile carries the fields the builder tray reads.
        first = data["members"][0]
        assert set(first) >= {
            "id",
            "asset_type",
            "name",
            "thumbnail_url",
            "duration_seconds",
        }
        assert first["asset_type"] == "image"

    async def test_excludes_ineligible_asset_types(self, client, db_session):
        import datetime as _dt
        import uuid as _uuid

        from cms.models.asset import Asset, AssetType

        img = (await _seed_assets(db_session, n=1))[0]
        # A WEBPAGE is not a tag-deck-eligible leaf type and must be filtered.
        web = Asset(
            filename="page.url",
            original_filename="page.url",
            asset_type=AssetType.WEBPAGE,
            size_bytes=10,
            checksum="webchk",
        )
        db_session.add(web)
        await db_session.commit()
        await db_session.refresh(web)

        t = await _create_tag(client, "mixed")
        tid = _uuid.UUID(t["id"])
        await self._tag_assets(
            db_session, tid, [img, web], _dt.datetime(2026, 1, 1, 12, 0, 0)
        )

        resp = await client.get(f"/api/tags/{t['id']}/members")
        assert resp.status_code == 200, resp.text
        ids = [m["id"] for m in resp.json()["members"]]
        assert ids == [str(img.id)]

    async def test_empty_tag_returns_no_members(self, client):
        t = await _create_tag(client, "lonely")
        resp = await client.get(f"/api/tags/{t['id']}/members")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 0
        assert data["members"] == []
