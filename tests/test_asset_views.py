"""Tests for the AssetView (saved-views) API."""

from __future__ import annotations

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient


@pytest_asyncio.fixture
async def second_client(app):
    """Authenticated client for a second non-admin user (operator)."""
    from cms.auth import hash_password
    from cms.database import get_db
    from cms.models.user import Role, User
    from sqlalchemy import select

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        role = (
            await db.execute(select(Role).where(Role.name == "Operator"))
        ).scalar_one()
        user = User(
            username="views-operator",
            email="views-op@test.com",
            display_name="Views Operator",
            password_hash=hash_password("oppass"),
            role_id=role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(user)
        await db.commit()
        break

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post(
            "/login",
            data={"username": "views-operator", "password": "oppass"},
            follow_redirects=False,
        )
        yield ac


async def _create_view(
    client,
    name: str,
    *,
    filters: dict | None = None,
    is_default: bool = False,
) -> dict:
    resp = await client.post(
        "/api/asset-views",
        json={
            "name": name,
            "filters": filters or {},
            "is_default": is_default,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


@pytest.mark.asyncio
class TestAssetViewCRUD:
    async def test_create_minimal_and_list(self, client):
        v = await _create_view(client, "My recent uploads")
        assert v["name"] == "My recent uploads"
        assert v["is_default"] is False
        assert v["filters"] == {}

        listing = (await client.get("/api/asset-views")).json()
        assert any(x["id"] == v["id"] for x in listing)

    async def test_create_with_filters_persists_subset(self, client):
        filters = {
            "type": "video",
            "tag_id": "abc123",
            "usage": "unused",
            "date_days": "1",
            "order": "-uploaded_at",
            "view_mode": "grid",
        }
        v = await _create_view(client, "Untagged videos", filters=filters)
        assert v["filters"]["type"] == "video"
        assert v["filters"]["tag_id"] == "abc123"
        assert v["filters"]["usage"] == "unused"
        assert v["filters"]["date_days"] == "1"
        assert v["filters"]["view_mode"] == "grid"
        # None-valued fields should not be stored.
        assert "q" not in v["filters"]

    async def test_create_strips_whitespace(self, client):
        v = await _create_view(client, "  My View  ")
        assert v["name"] == "My View"

    async def test_create_rejects_blank_name(self, client):
        resp = await client.post(
            "/api/asset-views", json={"name": "   ", "filters": {}}
        )
        assert resp.status_code == 422

    async def test_create_rejects_invalid_usage_value(self, client):
        resp = await client.post(
            "/api/asset-views",
            json={"name": "bad", "filters": {"usage": "sometimes"}},
        )
        assert resp.status_code == 422

    async def test_create_rejects_invalid_view_mode(self, client):
        resp = await client.post(
            "/api/asset-views",
            json={"name": "bad", "filters": {"view_mode": "kanban"}},
        )
        assert resp.status_code == 422

    async def test_create_rejects_duplicate_name_for_same_user(self, client):
        await _create_view(client, "dup")
        resp = await client.post(
            "/api/asset-views", json={"name": "dup", "filters": {}}
        )
        assert resp.status_code == 409

    async def test_patch_rename_and_filters(self, client):
        v = await _create_view(client, "old", filters={"type": "image"})
        resp = await client.patch(
            f"/api/asset-views/{v['id']}",
            json={
                "name": "renamed",
                "filters": {"type": "video", "usage": "used"},
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["name"] == "renamed"
        assert body["filters"]["type"] == "video"
        assert body["filters"]["usage"] == "used"

    async def test_patch_rejects_rename_to_existing_name(self, client):
        a = await _create_view(client, "alpha")
        b = await _create_view(client, "beta")
        resp = await client.patch(
            f"/api/asset-views/{b['id']}", json={"name": "alpha"}
        )
        assert resp.status_code == 409
        assert a["id"]  # touched to silence linter

    async def test_delete(self, client):
        v = await _create_view(client, "tmp")
        resp = await client.delete(f"/api/asset-views/{v['id']}")
        assert resp.status_code == 204
        listing = (await client.get("/api/asset-views")).json()
        assert not any(x["id"] == v["id"] for x in listing)


@pytest.mark.asyncio
class TestAssetViewDefaults:
    async def test_setting_default_clears_other_defaults(self, client):
        a = await _create_view(client, "A", is_default=True)
        b = await _create_view(client, "B")
        # Promote B to default via PATCH.
        resp = await client.patch(
            f"/api/asset-views/{b['id']}", json={"is_default": True}
        )
        assert resp.status_code == 200

        listing = {x["id"]: x for x in (await client.get("/api/asset-views")).json()}
        assert listing[a["id"]]["is_default"] is False
        assert listing[b["id"]]["is_default"] is True

    async def test_creating_default_clears_other_defaults(self, client):
        a = await _create_view(client, "A", is_default=True)
        b = await _create_view(client, "B", is_default=True)
        listing = {x["id"]: x for x in (await client.get("/api/asset-views")).json()}
        assert listing[a["id"]]["is_default"] is False
        assert listing[b["id"]]["is_default"] is True

    async def test_deleting_default_leaves_no_default(self, client):
        a = await _create_view(client, "A", is_default=True)
        await _create_view(client, "B")
        resp = await client.delete(f"/api/asset-views/{a['id']}")
        assert resp.status_code == 204
        listing = (await client.get("/api/asset-views")).json()
        assert all(x["is_default"] is False for x in listing)


@pytest.mark.asyncio
class TestAssetViewIsolation:
    async def test_users_cannot_see_each_others_views(self, client, second_client):
        v_admin = await _create_view(client, "Admin view")
        v_op = await _create_view(second_client, "Op view")

        admin_listing = (await client.get("/api/asset-views")).json()
        op_listing = (await second_client.get("/api/asset-views")).json()
        assert any(x["id"] == v_admin["id"] for x in admin_listing)
        assert not any(x["id"] == v_op["id"] for x in admin_listing)
        assert any(x["id"] == v_op["id"] for x in op_listing)
        assert not any(x["id"] == v_admin["id"] for x in op_listing)

    async def test_users_cannot_patch_each_others_views(self, client, second_client):
        v_op = await _create_view(second_client, "Op view")
        resp = await client.patch(
            f"/api/asset-views/{v_op['id']}", json={"name": "hijacked"}
        )
        assert resp.status_code == 404

    async def test_users_cannot_delete_each_others_views(self, client, second_client):
        v_op = await _create_view(second_client, "Op view")
        resp = await client.delete(f"/api/asset-views/{v_op['id']}")
        assert resp.status_code == 404
        # Confirm it still exists for the owner.
        listing = (await second_client.get("/api/asset-views")).json()
        assert any(x["id"] == v_op["id"] for x in listing)

    async def test_same_name_allowed_across_users(self, client, second_client):
        await _create_view(client, "Shared name")
        # Operator's same-name view should still be created (409 is per-user).
        v = await _create_view(second_client, "Shared name")
        assert v["name"] == "Shared name"
