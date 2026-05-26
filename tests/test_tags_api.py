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
