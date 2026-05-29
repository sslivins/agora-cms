"""Tests for MCP auth API and settings endpoints.

The MCP auth flow now uses user API keys (Bearer token → CMS validates
against api_keys table → returns user permissions).
"""

import pytest

from cms.auth import SETTING_MCP_ENABLED, _hash_api_key, set_setting
from cms.models.api_key import APIKey


async def _create_user_api_key(db_session, raw_key="agora_mcp_test_key_1234567890abcdef", key_type="mcp"):
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
        key_type=key_type,
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

    async def test_auth_rejects_api_type_key(self, client, db_session):
        """An API-type key should be rejected by the MCP auth endpoint."""
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        raw_key, _ = await _create_user_api_key(
            db_session,
            raw_key="agora_api_type_key_for_rest_only_1234",
            key_type="api",
        )

        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 403
        assert "MCP keys" in resp.json()["detail"]

    async def test_auth_returns_key_type(self, client, db_session):
        """Auth response should include the key_type field."""
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        raw_key, _ = await _create_user_api_key(db_session)

        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": f"Bearer {raw_key}"},
        )
        assert resp.status_code == 200
        assert resp.json()["key_type"] == "mcp"


@pytest.mark.asyncio
class TestMcpAuthOnBehalfOf:
    """Service-key + X-On-Behalf-Of auth path (used by the Assistant agent).

    Mode 2 from the docstring on verify_mcp_token:  the CMS sends its
    own service key as the bearer AND identifies the target user via
    the X-On-Behalf-Of header.  The endpoint must return that user's
    permissions — never any synthetic "service" permission set — and
    must reject the request if the service key is wrong, the header
    is missing or malformed, or the named user doesn't exist / is
    disabled.
    """

    async def _seed_service_key(self, db_session):
        """Install a known service key hash and return the raw key."""
        from cms.auth import (
            SETTING_MCP_SERVICE_KEY_HASH,
            SERVICE_KEY_PREFIX,
        )
        raw = SERVICE_KEY_PREFIX + "test_service_key_value_1234567890"
        await set_setting(db_session, SETTING_MCP_SERVICE_KEY_HASH,
                          _hash_api_key(raw))
        return raw

    async def _create_operator_user(self, db_session):
        from sqlalchemy import select
        from cms.auth import hash_password
        from cms.models.user import Role, User

        role = (await db_session.execute(
            select(Role).where(Role.name == "Operator")
        )).scalar_one()
        user = User(
            username="op-on-behalf-of",
            email="op-obo@test.com",
            display_name="Op OBO",
            password_hash=hash_password("x"),
            role_id=role.id,
            is_active=True,
            must_change_password=False,
        )
        db_session.add(user)
        await db_session.commit()
        await db_session.refresh(user)
        return user

    async def test_service_key_with_admin_obo_returns_admin_perms(
        self, client, db_session
    ):
        from sqlalchemy import select
        from cms.models.user import User

        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        service_key = await self._seed_service_key(db_session)
        admin = (await db_session.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()

        resp = await client.get(
            "/api/mcp/auth",
            headers={
                "Authorization": f"Bearer {service_key}",
                "X-On-Behalf-Of": str(admin.id),
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data["valid"] is True
        assert data["key_type"] == "service"
        assert data["on_behalf_of"] == str(admin.id)
        # Admin gets all permissions
        assert "devices:read" in data["permissions"]
        assert "users:write" in data["permissions"]

    async def test_service_key_with_operator_obo_returns_operator_perms(
        self, client, db_session
    ):
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        service_key = await self._seed_service_key(db_session)
        op = await self._create_operator_user(db_session)

        resp = await client.get(
            "/api/mcp/auth",
            headers={
                "Authorization": f"Bearer {service_key}",
                "X-On-Behalf-Of": str(op.id),
            },
        )
        assert resp.status_code == 200, resp.text
        data = resp.json()
        # Operator does NOT have admin-only permissions even when the
        # request comes via the trusted CMS service key.
        assert "users:write" not in data["permissions"]
        assert "roles:write" not in data["permissions"]
        # But still has the basic read permissions Operator role has.
        assert "devices:read" in data["permissions"]

    async def test_service_key_without_obo_header_rejected(
        self, client, db_session
    ):
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        service_key = await self._seed_service_key(db_session)
        resp = await client.get(
            "/api/mcp/auth",
            headers={"Authorization": f"Bearer {service_key}"},
        )
        assert resp.status_code == 400
        assert "X-On-Behalf-Of" in resp.json()["detail"]

    async def test_wrong_service_key_rejected(self, client, db_session):
        from cms.auth import SERVICE_KEY_PREFIX
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        await self._seed_service_key(db_session)  # plant the correct hash
        wrong = SERVICE_KEY_PREFIX + "definitely_not_the_real_one_nope"
        import uuid
        resp = await client.get(
            "/api/mcp/auth",
            headers={
                "Authorization": f"Bearer {wrong}",
                "X-On-Behalf-Of": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 401

    async def test_obo_with_non_uuid_rejected(self, client, db_session):
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        service_key = await self._seed_service_key(db_session)
        resp = await client.get(
            "/api/mcp/auth",
            headers={
                "Authorization": f"Bearer {service_key}",
                "X-On-Behalf-Of": "not-a-uuid",
            },
        )
        assert resp.status_code == 400

    async def test_obo_with_unknown_user_404s(self, client, db_session):
        import uuid
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        service_key = await self._seed_service_key(db_session)
        resp = await client.get(
            "/api/mcp/auth",
            headers={
                "Authorization": f"Bearer {service_key}",
                "X-On-Behalf-Of": str(uuid.uuid4()),
            },
        )
        assert resp.status_code == 404

    async def test_obo_with_disabled_user_403s(self, client, db_session):
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")
        service_key = await self._seed_service_key(db_session)
        op = await self._create_operator_user(db_session)
        op.is_active = False
        await db_session.commit()

        resp = await client.get(
            "/api/mcp/auth",
            headers={
                "Authorization": f"Bearer {service_key}",
                "X-On-Behalf-Of": str(op.id),
            },
        )
        assert resp.status_code == 403

    async def test_mcp_disabled_blocks_service_key_path(
        self, client, db_session
    ):
        from sqlalchemy import select
        from cms.models.user import User

        # MCP off — even valid service-key + OBO should 403.
        await set_setting(db_session, SETTING_MCP_ENABLED, "false")
        service_key = await self._seed_service_key(db_session)
        admin = (await db_session.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        resp = await client.get(
            "/api/mcp/auth",
            headers={
                "Authorization": f"Bearer {service_key}",
                "X-On-Behalf-Of": str(admin.id),
            },
        )
        assert resp.status_code == 403


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
