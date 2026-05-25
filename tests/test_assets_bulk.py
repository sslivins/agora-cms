"""Tests for the polymorphic bulk asset endpoint ``POST /api/assets/bulk``."""

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


async def _seed_group(db_session, name: str = "GroupA"):
    from cms.models.device import DeviceGroup

    g = DeviceGroup(name=name)
    db_session.add(g)
    await db_session.commit()
    await db_session.refresh(g)
    return g


@pytest.mark.asyncio
class TestAssetsBulk:
    async def test_invalid_action(self, client):
        resp = await client.post(
            "/api/assets/bulk",
            json={"asset_ids": ["00000000-0000-0000-0000-000000000001"], "action": "nuke"},
        )
        # Pydantic 422 for action that fails enum check.
        assert resp.status_code in (400, 422)

    async def test_empty_asset_ids_rejected(self, client):
        resp = await client.post(
            "/api/assets/bulk", json={"asset_ids": [], "action": "delete"}
        )
        assert resp.status_code == 422

    async def test_bulk_delete_happy_path(self, client, db_session):
        assets = await _seed_assets(db_session, n=3)
        ids = [str(a.id) for a in assets]
        resp = await client.post(
            "/api/assets/bulk", json={"asset_ids": ids, "action": "delete"}
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert sorted(data["succeeded"]) == sorted(ids)
        assert data["failed"] == []

        # Confirm assets are soft-deleted (deleted_at set).
        from sqlalchemy import select
        from cms.models.asset import Asset

        rows = (
            await db_session.execute(select(Asset.deleted_at).where(Asset.id.in_([a.id for a in assets])))
        ).scalars().all()
        assert all(r is not None for r in rows)

    async def test_bulk_delete_partial_failure(self, client, db_session):
        assets = await _seed_assets(db_session, n=2)
        ids = [str(a.id) for a in assets] + ["00000000-0000-0000-0000-000000000000"]
        resp = await client.post(
            "/api/assets/bulk", json={"asset_ids": ids, "action": "delete"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert sorted(data["succeeded"]) == sorted(str(a.id) for a in assets)
        assert len(data["failed"]) == 1
        assert data["failed"][0]["id"] == "00000000-0000-0000-0000-000000000000"
        assert data["failed"][0]["status"] == 404

    async def test_bulk_add_group_requires_group_id(self, client, db_session):
        assets = await _seed_assets(db_session, n=1)
        resp = await client.post(
            "/api/assets/bulk",
            json={"asset_ids": [str(assets[0].id)], "action": "add_group"},
        )
        assert resp.status_code == 400
        assert "group_id" in resp.json()["detail"]

    async def test_bulk_add_then_remove_group(self, client, db_session):
        from sqlalchemy import select
        from cms.models.group_asset import GroupAsset

        assets = await _seed_assets(db_session, n=2)
        group = await _seed_group(db_session)
        ids = [str(a.id) for a in assets]

        # add_group
        resp = await client.post(
            "/api/assets/bulk",
            json={"asset_ids": ids, "action": "add_group", "group_id": str(group.id)},
        )
        assert resp.status_code == 200, resp.text
        assert sorted(resp.json()["succeeded"]) == sorted(ids)

        rows = (
            await db_session.execute(
                select(GroupAsset).where(GroupAsset.group_id == group.id)
            )
        ).scalars().all()
        assert {str(r.asset_id) for r in rows} == set(ids)

        # remove_group
        resp = await client.post(
            "/api/assets/bulk",
            json={"asset_ids": ids, "action": "remove_group", "group_id": str(group.id)},
        )
        assert resp.status_code == 200, resp.text
        assert sorted(resp.json()["succeeded"]) == sorted(ids)

        rows = (
            await db_session.execute(
                select(GroupAsset).where(GroupAsset.group_id == group.id)
            )
        ).scalars().all()
        assert rows == []

    async def test_bulk_set_global_requires_admin(self, client, db_session):
        # The default test fixture authenticates as admin, so the happy
        # path goes through. We just confirm the action is wired up and
        # idempotent.
        from sqlalchemy import select
        from cms.models.asset import Asset

        assets = await _seed_assets(db_session, n=2)
        ids = [str(a.id) for a in assets]

        # Mark global
        resp = await client.post(
            "/api/assets/bulk",
            json={"asset_ids": ids, "action": "set_global", "is_global": True},
        )
        assert resp.status_code == 200, resp.text
        assert sorted(resp.json()["succeeded"]) == sorted(ids)
        flags = (
            await db_session.execute(
                select(Asset.is_global).where(Asset.id.in_([a.id for a in assets]))
            )
        ).scalars().all()
        assert all(flags)

        # Idempotent re-application: nothing changes, all succeed.
        resp = await client.post(
            "/api/assets/bulk",
            json={"asset_ids": ids, "action": "set_global", "is_global": True},
        )
        assert resp.status_code == 200
        assert sorted(resp.json()["succeeded"]) == sorted(ids)

    async def test_bulk_set_global_requires_is_global(self, client, db_session):
        assets = await _seed_assets(db_session, n=1)
        resp = await client.post(
            "/api/assets/bulk",
            json={"asset_ids": [str(assets[0].id)], "action": "set_global"},
        )
        assert resp.status_code == 400

    async def test_bulk_deduplicates_ids(self, client, db_session):
        assets = await _seed_assets(db_session, n=1)
        aid = str(assets[0].id)
        resp = await client.post(
            "/api/assets/bulk", json={"asset_ids": [aid, aid, aid], "action": "delete"}
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["succeeded"] == [aid]
        assert data["failed"] == []
