"""Tests for the CMS UI overhaul: header status lights, profile password,
settings page cleanup, version footer, and system:health permission."""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from unittest.mock import patch, AsyncMock

from cms.auth import hash_password, set_setting
from cms.models.user import Role, User


# ── Helpers ──


async def _get_role_id(db: AsyncSession, name: str) -> uuid.UUID:
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one().id


async def _create_user(
    db: AsyncSession, *, username: str, role_name: str = "Viewer",
) -> User:
    role_id = await _get_role_id(db, role_name)
    user = User(
        username=username,
        email=f"{username}@test.com",
        display_name=username.title(),
        password_hash=hash_password("password123"),
        role_id=role_id,
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user, ["role"])
    return user


async def _login_as(app, username: str) -> AsyncClient:
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    await ac.post("/login", data={"username": username, "password": "password123"},
                  follow_redirects=False)
    return ac


# ═══════════════════════════════════════════════════════════════════
# 1. system:health permission
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestSystemHealthPermission:

    def test_system_health_in_all_permissions(self):
        from cms.permissions import ALL_PERMISSIONS, SYSTEM_HEALTH
        assert SYSTEM_HEALTH in ALL_PERMISSIONS

    def test_system_health_in_admin_permissions(self):
        from cms.permissions import ADMIN_PERMISSIONS, SYSTEM_HEALTH
        assert SYSTEM_HEALTH in ADMIN_PERMISSIONS

    def test_system_health_not_in_operator(self):
        from cms.permissions import OPERATOR_PERMISSIONS, SYSTEM_HEALTH
        assert SYSTEM_HEALTH not in OPERATOR_PERMISSIONS

    def test_system_health_not_in_viewer(self):
        from cms.permissions import VIEWER_PERMISSIONS, SYSTEM_HEALTH
        assert SYSTEM_HEALTH not in VIEWER_PERMISSIONS

    def test_system_health_description_exists(self):
        from cms.permissions import PERMISSION_DESCRIPTIONS, SYSTEM_HEALTH
        assert SYSTEM_HEALTH in PERMISSION_DESCRIPTIONS
        assert len(PERMISSION_DESCRIPTIONS[SYSTEM_HEALTH]) > 0


# ═══════════════════════════════════════════════════════════════════
# 2. Health API endpoint
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestHealthEndpoint:

    async def test_health_requires_auth(self, unauthed_client):
        resp = await unauthed_client.get("/api/system/health")
        assert resp.status_code == 401

    async def test_health_returns_structure(self, client):
        resp = await client.get("/api/system/health")
        assert resp.status_code == 200
        data = resp.json()
        assert "db" in data
        assert "online" in data["db"]
        assert "smtp" in data
        assert "configured" in data["smtp"]
        assert "mcp" in data
        assert "enabled" in data["mcp"]

    async def test_health_db_check(self, client):
        """In SQLite test env, DB should still report online (SELECT 1 works)."""
        resp = await client.get("/api/system/health")
        data = resp.json()
        assert data["db"]["online"] is True

    async def test_health_smtp_unconfigured_by_default(self, client):
        """No SMTP configured in test env — should report unconfigured."""
        resp = await client.get("/api/system/health")
        data = resp.json()
        assert data["smtp"]["configured"] is False

    async def test_health_smtp_configured(self, client, db_session):
        """When SMTP host is set, should report configured."""
        await set_setting(db_session, "smtp_host", "smtp.example.com")
        await db_session.commit()
        resp = await client.get("/api/system/health")
        data = resp.json()
        assert data["smtp"]["configured"] is True

    async def test_health_mcp_disabled_by_default(self, client):
        """MCP disabled by default in test env."""
        resp = await client.get("/api/system/health")
        data = resp.json()
        assert data["mcp"]["enabled"] is False


# ═══════════════════════════════════════════════════════════════════
# 3. Header status lights (conditional rendering by role)
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestHeaderStatusLights:

    async def test_admin_sees_all_lights(self, client):
        """Admin (system:health + mcp_keys:self) sees DB, SMTP, and MCP dots."""
        resp = await client.get("/")
        text = resp.text
        assert 'id="status-db"' in text
        assert 'id="status-smtp"' in text
        assert 'id="status-mcp"' in text

    async def test_operator_sees_mcp_only(self, app, db_session):
        """Operator has mcp_keys:self but NOT system:health."""
        await _create_user(db_session, username="oper1", role_name="Operator")
        ac = await _login_as(app, "oper1")
        try:
            resp = await ac.get("/")
            text = resp.text
            assert 'id="status-db"' not in text
            assert 'id="status-smtp"' not in text
            assert 'id="status-mcp"' in text
        finally:
            await ac.aclose()

    async def test_viewer_sees_no_lights(self, app, db_session):
        """Viewer has neither system:health nor mcp_keys:self."""
        await _create_user(db_session, username="viewer1", role_name="Viewer")
        ac = await _login_as(app, "viewer1")
        try:
            resp = await ac.get("/")
            text = resp.text
            assert 'id="status-db"' not in text
            assert 'id="status-smtp"' not in text
            assert 'id="status-mcp"' not in text
        finally:
            await ac.aclose()

    async def test_polling_js_present_for_admin(self, client):
        """Admin pages include the pollSystemHealth JS function."""
        resp = await client.get("/")
        assert "pollSystemHealth" in resp.text

    async def test_polling_js_present_for_viewer(self, app, db_session):
        """Even viewer pages include the JS (it no-ops if no dots rendered)."""
        await _create_user(db_session, username="viewer2", role_name="Viewer")
        ac = await _login_as(app, "viewer2")
        try:
            resp = await ac.get("/")
            assert "pollSystemHealth" in resp.text
        finally:
            await ac.aclose()


# ═══════════════════════════════════════════════════════════════════
# 4. Profile password change
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestProfilePasswordChange:

    async def test_profile_has_security_tab(self, client):
        """Profile page renders the Security tab with password form."""
        resp = await client.get("/profile")
        assert resp.status_code == 200
        assert "Security" in resp.text
        assert "current_password" in resp.text
        assert "new_password" in resp.text

    async def test_password_change_success(self, client):
        resp = await client.post("/profile/password", data={
            "current_password": "testpass",
            "new_password": "newpass123",
            "confirm_password": "newpass123",
        }, follow_redirects=False)
        assert resp.status_code == 200
        assert "Password updated" in resp.text

    async def test_password_change_wrong_current(self, client):
        resp = await client.post("/profile/password", data={
            "current_password": "wrongpass",
            "new_password": "newpass123",
            "confirm_password": "newpass123",
        }, follow_redirects=False)
        assert resp.status_code == 400
        assert "incorrect" in resp.text.lower() or "current password" in resp.text.lower()

    async def test_password_change_mismatch(self, client):
        resp = await client.post("/profile/password", data={
            "current_password": "testpass",
            "new_password": "newpass123",
            "confirm_password": "different456",
        }, follow_redirects=False)
        assert resp.status_code == 400
        assert "match" in resp.text.lower()

    async def test_password_change_too_short(self, client):
        resp = await client.post("/profile/password", data={
            "current_password": "testpass",
            "new_password": "abc",
            "confirm_password": "abc",
        }, follow_redirects=False)
        assert resp.status_code == 400
        assert "6" in resp.text# "at least 6 characters"

    async def test_password_change_requires_auth(self, unauthed_client):
        resp = await unauthed_client.post("/profile/password", data={
            "current_password": "testpass",
            "new_password": "newpass123",
            "confirm_password": "newpass123",
        }, follow_redirects=False)
        assert resp.status_code in (401, 303)  # 401 or redirect to login


# ═══════════════════════════════════════════════════════════════════
# 5. Version in footer
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestVersionFooter:

    async def test_footer_shows_version(self, client):
        from cms import __version__
        resp = await client.get("/")
        assert resp.status_code == 200
        assert __version__ in resp.text

    async def test_footer_in_profile_page(self, client):
        from cms import __version__
        resp = await client.get("/profile")
        assert __version__ in resp.text

    async def test_version_element_present(self, client):
        resp = await client.get("/")
        assert "nav-version" in resp.text


# ═══════════════════════════════════════════════════════════════════
# 6. Settings page cleanup
# ═══════════════════════════════════════════════════════════════════


@pytest.mark.asyncio
class TestSettingsPageCleanup:

    async def test_settings_requires_settings_write(self, app, db_session):
        """Viewer (no settings:write) gets 403 on settings page."""
        await _create_user(db_session, username="viewer3", role_name="Viewer")
        ac = await _login_as(app, "viewer3")
        try:
            resp = await ac.get("/settings", follow_redirects=False)
            assert resp.status_code == 403
        finally:
            await ac.aclose()

    async def test_operator_cannot_access_settings(self, app, db_session):
        """Operator (no settings:write) gets 403 on settings page."""
        await _create_user(db_session, username="oper2", role_name="Operator")
        ac = await _login_as(app, "oper2")
        try:
            resp = await ac.get("/settings", follow_redirects=False)
            assert resp.status_code == 403
        finally:
            await ac.aclose()

    async def test_admin_can_access_settings(self, client):
        """Admin (settings:write) can access settings."""
        resp = await client.get("/settings")
        assert resp.status_code == 200

    async def test_settings_gear_hidden_for_non_admin(self, app, db_session):
        """Settings link should not appear for users without settings:write."""
        await _create_user(db_session, username="viewer4", role_name="Viewer")
        ac = await _login_as(app, "viewer4")
        try:
            resp = await ac.get("/")
            assert 'href="/settings"' not in resp.text
        finally:
            await ac.aclose()

    async def test_settings_gear_visible_for_admin(self, client):
        """Admin sees the settings link in header."""
        resp = await client.get("/")
        assert 'href="/settings"' in resp.text

    async def test_removed_system_info_card(self, client):
        """System Info card no longer exists on settings page."""
        resp = await client.get("/settings")
        assert "System Info" not in resp.text

    async def test_removed_db_password_card(self, client):
        """Database card / Change Password card removed from settings."""
        resp = await client.get("/settings")
        assert "Change Database Password" not in resp.text
        assert "db-status-badge" not in resp.text

    async def test_remaining_sections_present(self, client):
        """MCP, SMTP, and Download Logs sections still present."""
        resp = await client.get("/settings")
        text = resp.text
        assert "MCP" in text
        assert "SMTP" in text or "smtp" in text

    async def test_db_change_password_endpoint_removed(self, client):
        """POST /api/db/change-password should 404 or 405."""
        resp = await client.post(
            "/api/db/change-password",
            json={"password": "new-secure-password"},
        )
        assert resp.status_code in (404, 405)

    async def test_old_settings_password_endpoint_removed(self, client):
        """POST /settings/password (old route) should 404 or 405."""
        resp = await client.post(
            "/settings/password",
            data={"current_password": "x", "new_password": "y", "confirm_password": "y"},
        )
        assert resp.status_code in (404, 405)

    async def test_timezone_post_requires_settings_write(self, app, db_session):
        """POST /settings/timezone is gated to settings:write."""
        await _create_user(db_session, username="viewer5", role_name="Viewer")
        ac = await _login_as(app, "viewer5")
        try:
            resp = await ac.post("/settings/timezone",
                                 data={"timezone": "UTC"},
                                 follow_redirects=False)
            assert resp.status_code == 403
        finally:
            await ac.aclose()

    async def test_timezone_works_for_admin(self, client):
        """Admin can still change timezone."""
        resp = await client.post("/settings/timezone",
                                 data={"timezone": "America/New_York"},
                                 follow_redirects=False)
        assert resp.status_code == 200
        assert "America/New_York" in resp.text
