"""Tests for MCP auth API and settings endpoints.

The MCP auth flow now uses user API keys (Bearer token → CMS validates
against api_keys table → returns user permissions).
"""

import pytest

from cms.auth import SETTING_MCP_ENABLED, _hash_api_key, set_setting
from cms.models.api_key import APIKey


async def _create_user_api_key(db_session, raw_key="agora_mcp_test_key_1234567890abcdef"):
    """Helper: create an API key linked to the admin user."""
    from sqlalchemy import select
    from cms.models.user import User

    # Find admin user seeded by conftest
    result = await db_session.execute(select(User).where(User.username == "admin"))
    admin = result.scalar_one()

    key_hash = _hash_api_key(raw_key)
    api_key = APIKey(
        name="MCP Test Key",
        key_prefix=raw_key[:12] + "...",
        key_hash=key_hash,
        user_id=admin.id,
    )
    db_session.add(api_key)
    await db_session.commit()
    return raw_key, admin


@pytest.mark.asyncio
class TestMcpAuth:
    """Test the /api/mcp/auth endpoint used by the MCP server."""

    async def test_auth_valid_user_api_key(self, client, db_session):
        """A valid user API key should return user info and permissions."""
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        raw_key, admin = await _create_user_api_key(db_session)

        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["role"] == "Admin"
        assert "devices:read" in data["permissions"]
        assert "users:write" in data["permissions"]

    async def test_auth_invalid_key(self, client, db_session):
        """An invalid API key should be rejected with 401."""
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")

        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": "Bearer agora_wrong_key_not_real"},
        )
        assert resp.status_code == 401

    async def test_auth_missing_token(self, client, db_session):
        """Missing Authorization header should return 401."""
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")

        resp = await client.get("/api/mcp/auth")
        assert resp.status_code == 401

    async def test_auth_mcp_disabled(self, client, db_session):
        """When MCP is disabled, auth should return 403 even with a valid key."""
        await set_setting(db_session, SETTING_MCP_ENABLED, "false")
        raw_key, _ = await _create_user_api_key(db_session)

        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 403

    async def test_auth_inactive_user(self, client, db_session):
        """A key belonging to a disabled user should be rejected."""
        from sqlalchemy import select
        from cms.models.user import User

        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        raw_key, admin = await _create_user_api_key(db_session)

        # Disable the admin user
        result = await db_session.execute(select(User).where(User.id == admin.id))
        user = result.scalar_one()
        user.is_active = False
        await db_session.commit()

        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 403
        assert "disabled" in resp.json()["detail"].lower()

    async def test_auth_returns_permissions_list(self, client, db_session):
        """Auth response should include the full permissions list for the user's role."""
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        raw_key, _ = await _create_user_api_key(db_session)

        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        data = resp.json()
        perms = data["permissions"]
        assert isinstance(perms, list)
        assert len(perms) > 0
        # Admin should have all permissions
        assert "devices:read" in perms
        assert "devices:write" in perms
        assert "audit:read" in perms


@pytest.mark.asyncio
class TestMcpSettings:
    """Test the MCP settings UI endpoints."""

    async def test_toggle_enable(self, client, db_session):
        resp = await client.post(
            "/api/mcp/toggle",
            json={"enabled": True},
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is True

        from cms.auth import get_setting
        val = await get_setting(db_session, SETTING_MCP_ENABLED)
        assert val == "true"

    async def test_toggle_disable(self, client, db_session):
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")

        resp = await client.post(
            "/api/mcp/toggle",
            json={"enabled": False},
        )
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

        from cms.auth import get_setting
        val = await get_setting(db_session, SETTING_MCP_ENABLED)
        assert val == "false"

    async def test_generate_key_endpoint_removed(self, client, db_session):
        """The old /api/mcp/generate-key endpoint should no longer exist."""
        resp = await client.post("/api/mcp/generate-key")
        assert resp.status_code in (404, 405)

    async def test_settings_page_shows_mcp(self, client, db_session):
        resp = await client.get("/settings")
        assert resp.status_code == 200
        text = resp.text
        assert "MCP Server" in text
        assert "mcp-enabled" in text
        # Old generate button should be gone
        assert "mcp-generate-key" not in text
