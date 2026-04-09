"""Tests for authentication."""

import pytest


@pytest.mark.asyncio
class TestAuth:
    async def test_login_success(self, unauthed_client):
        resp = await unauthed_client.post(
            "/login",
            data={"username": "admin", "password": "testpass"},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "agora_cms_session" in resp.cookies

    async def test_login_wrong_password(self, unauthed_client):
        resp = await unauthed_client.post(
            "/login",
            data={"username": "admin", "password": "wrong"},
            follow_redirects=False,
        )
        # Should not set cookie, returns error page
        assert "agora_cms_session" not in resp.cookies

    async def test_login_wrong_username(self, unauthed_client):
        resp = await unauthed_client.post(
            "/login",
            data={"username": "hacker", "password": "testpass"},
            follow_redirects=False,
        )
        assert "agora_cms_session" not in resp.cookies

    async def test_protected_route_without_auth(self, unauthed_client):
        resp = await unauthed_client.get("/api/devices")
        assert resp.status_code in (401, 303)

    async def test_protected_route_with_auth(self, client):
        resp = await client.get("/api/devices")
        assert resp.status_code == 200

    async def test_logout(self, client):
        resp = await client.get("/logout", follow_redirects=False)
        assert resp.status_code == 303

    async def test_login_page_accessible(self, unauthed_client):
        resp = await unauthed_client.get("/login")
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestAPIKeyAuth:
    """Test that API key authentication works alongside session cookies."""

    async def test_api_key_grants_access(self, app, db_session):
        """A valid API key in X-API-Key header should grant access."""
        from cms.auth import _hash_api_key
        from cms.models.api_key import APIKey

        raw_key = "agora_test1234567890abcdef1234567890abcdef"
        key_hash = _hash_api_key(raw_key)
        api_key = APIKey(name="Test Key", key_prefix="agora_test12...", key_hash=key_hash)
        db_session.add(api_key)
        await db_session.commit()

        from httpx import ASGITransport, AsyncClient
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/devices", headers={"X-API-Key": raw_key})
            assert resp.status_code == 200

    async def test_invalid_api_key_rejected(self, app):
        """An invalid API key should be rejected with 401."""
        from httpx import ASGITransport, AsyncClient
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/devices", headers={"X-API-Key": "agora_invalid"})
            assert resp.status_code == 401

    async def test_session_cookie_still_works(self, client):
        """Session cookie auth should continue to work."""
        resp = await client.get("/api/devices")
        assert resp.status_code == 200


@pytest.mark.asyncio
class TestAPIKeyCRUD:
    """Test API key management endpoints."""

    async def test_create_key(self, client):
        """POST /api/keys creates a new key and returns the raw key."""
        resp = await client.post("/api/keys", json={"name": "Test Key"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Test Key"
        assert data["key"].startswith("agora_")
        assert data["key_prefix"].endswith("...")

    async def test_list_keys_hides_raw(self, client):
        """GET /api/keys returns keys without raw values."""
        await client.post("/api/keys", json={"name": "Listed Key"})
        resp = await client.get("/api/keys")
        assert resp.status_code == 200
        for k in resp.json():
            assert "key" not in k

    async def test_delete_key(self, client):
        """DELETE /api/keys/{id} removes the key."""
        create_resp = await client.post("/api/keys", json={"name": "Ephemeral"})
        key_id = create_resp.json()["id"]
        del_resp = await client.delete(f"/api/keys/{key_id}")
        assert del_resp.status_code == 200

    async def test_created_key_authenticates(self, client, app):
        """A newly created key should work for authentication."""
        create_resp = await client.post("/api/keys", json={"name": "Functional"})
        raw_key = create_resp.json()["key"]

        from httpx import ASGITransport, AsyncClient
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/devices", headers={"X-API-Key": raw_key})
            assert resp.status_code == 200

    async def test_deleted_key_stops_working(self, client, app):
        """After deleting a key, it should no longer authenticate."""
        create_resp = await client.post("/api/keys", json={"name": "Revocable"})
        data = create_resp.json()

        await client.delete(f"/api/keys/{data['id']}")

        from httpx import ASGITransport, AsyncClient
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/devices", headers={"X-API-Key": data["key"]})
            assert resp.status_code == 401


@pytest.mark.asyncio
class TestUIPages:
    async def test_dashboard(self, client):
        resp = await client.get("/", follow_redirects=False)
        assert resp.status_code == 200

    async def test_devices_page(self, client):
        resp = await client.get("/devices", follow_redirects=False)
        assert resp.status_code == 200

    async def test_assets_page(self, client):
        resp = await client.get("/assets", follow_redirects=False)
        assert resp.status_code == 200

    async def test_schedules_page(self, client):
        resp = await client.get("/schedules", follow_redirects=False)
        assert resp.status_code == 200
