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
