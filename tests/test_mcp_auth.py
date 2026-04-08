"""Tests for MCP auth API and settings endpoints."""

import pytest


@pytest.mark.asyncio
class TestMcpAuth:
    """Test the /api/mcp/auth endpoint used by the MCP server."""

    async def _enable_mcp(self, db_session, api_key="test-key-123"):
        from cms.auth import SETTING_MCP_API_KEY, SETTING_MCP_ENABLED, set_setting
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        await set_setting(db_session, SETTING_MCP_API_KEY, api_key)

    async def test_auth_valid_token(self, client, db_session):
        await self._enable_mcp(db_session, api_key="valid-key")

        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": "Bearer valid-key"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["valid"] is True
        assert data["role"] == "admin"

    async def test_auth_invalid_token(self, client, db_session):
        await self._enable_mcp(db_session, api_key="correct-key")

        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": "Bearer wrong-key"},
        )
        assert resp.status_code == 401

    async def test_auth_missing_token(self, client, db_session):
        await self._enable_mcp(db_session)

        resp = await client.get("/api/mcp/auth")
        assert resp.status_code == 401

    async def test_auth_mcp_disabled(self, client, db_session):
        from cms.auth import SETTING_MCP_API_KEY, SETTING_MCP_ENABLED, set_setting
        await set_setting(db_session, SETTING_MCP_ENABLED, "false")
        await set_setting(db_session, SETTING_MCP_API_KEY, "some-key")

        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": "Bearer some-key"},
        )
        assert resp.status_code == 403

    async def test_auth_no_key_configured(self, client, db_session):
        from cms.auth import SETTING_MCP_ENABLED, set_setting
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        # No key set

        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": "Bearer anything"},
        )
        assert resp.status_code == 401


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

        # Verify it persisted
        from cms.auth import SETTING_MCP_ENABLED, get_setting
        val = await get_setting(db_session, SETTING_MCP_ENABLED)
        assert val == "true"

    async def test_toggle_disable(self, client, db_session):
        from cms.auth import SETTING_MCP_ENABLED, set_setting
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

    async def test_generate_key(self, client, db_session):
        resp = await client.post("/api/mcp/generate-key")
        assert resp.status_code == 200
        data = resp.json()
        assert "key" in data
        assert len(data["key"]) > 20  # token_urlsafe(32) produces ~43 chars

        # Verify it persisted
        from cms.auth import SETTING_MCP_API_KEY, get_setting
        stored = await get_setting(db_session, SETTING_MCP_API_KEY)
        assert stored == data["key"]

    async def test_regenerate_key_replaces_old(self, client, db_session):
        resp1 = await client.post("/api/mcp/generate-key")
        key1 = resp1.json()["key"]

        resp2 = await client.post("/api/mcp/generate-key")
        key2 = resp2.json()["key"]

        assert key1 != key2

        from cms.auth import SETTING_MCP_API_KEY, get_setting
        stored = await get_setting(db_session, SETTING_MCP_API_KEY)
        assert stored == key2

    async def test_settings_page_shows_mcp(self, client, db_session):
        resp = await client.get("/settings")
        assert resp.status_code == 200
        text = resp.text
        assert "MCP Server" in text
        assert "mcp-enabled" in text
