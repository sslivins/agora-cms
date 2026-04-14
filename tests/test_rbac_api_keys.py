"""Comprehensive RBAC tests for API key management.

Covers:
- Key type enforcement (MCP keys blocked on REST API, API keys allowed)
- Permission intersection / ceiling logic (compute_effective_permissions)
- Self-service key endpoints (/api/keys/my CRUD)
- Admin key management endpoints (/api/keys CRUD)
- IDOR protection (users can't touch other users' keys)
- Audit logging and notifications for key operations
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import (
    SETTING_API_ROLE_ID,
    SETTING_MCP_ROLE_ID,
    _hash_api_key,
    compute_effective_permissions,
    hash_password,
    set_setting,
)
from cms.models.api_key import APIKey
from cms.models.notification import Notification
from cms.models.user import Role, User, UserGroup


# ── Helpers ──────────────────────────────────────────────────────────


async def _get_role_id(db: AsyncSession, name: str) -> uuid.UUID:
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one().id


async def _get_role(db: AsyncSession, name: str) -> Role:
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one()


async def _create_user(
    db: AsyncSession, *, email: str, role_name: str = "Viewer",
    display_name: str | None = None,
) -> User:
    """Create a test user with the given role."""
    from sqlalchemy.orm import selectinload
    role_id = await _get_role_id(db, role_name)
    username = email.split("@")[0]
    user = User(
        username=username,
        email=email,
        display_name=display_name or username,
        password_hash=hash_password("password123"),
        role_id=role_id,
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user, ["role"])
    return user


async def _login_as(app, user_email: str) -> AsyncClient:
    """Return an authenticated AsyncClient logged in as the given user."""
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    username = user_email.split("@")[0]
    await ac.post("/login", data={"username": username, "password": "password123"},
                  follow_redirects=False)
    return ac


async def _create_db_key(
    db: AsyncSession, user_id: uuid.UUID, *, key_type: str = "api", name: str = "Test Key",
) -> tuple[APIKey, str]:
    """Create an API key directly in the DB. Returns (key_row, raw_key)."""
    raw_key = "agora_test_" + uuid.uuid4().hex[:38]
    key = APIKey(
        name=name,
        key_prefix=raw_key[:12] + "...",
        key_hash=_hash_api_key(raw_key),
        key_type=key_type,
        user_id=user_id,
    )
    db.add(key)
    await db.commit()
    await db.refresh(key)
    return key, raw_key


# ── Key type enforcement ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestKeyTypeEnforcement:
    """MCP keys must be blocked on the REST API; API keys must work."""

    async def test_mcp_key_rejected_on_rest_api(self, app, db_session):
        """An MCP-type key should get 403 when used on REST endpoints."""
        admin = (await db_session.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        key_row, raw_key = await _create_db_key(db_session, admin.id, key_type="mcp",
                                                 name="MCP Key Blocked")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/devices", headers={"X-API-Key": raw_key})
            assert resp.status_code == 403
            assert "MCP keys cannot access the REST API" in resp.json()["detail"]

    async def test_api_key_works_on_rest_api(self, app, db_session):
        """A normal API-type key should authenticate fine on REST endpoints."""
        admin = (await db_session.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        key_row, raw_key = await _create_db_key(db_session, admin.id, key_type="api",
                                                 name="API Key Works")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/devices", headers={"X-API-Key": raw_key})
            assert resp.status_code == 200

    async def test_invalid_key_returns_401(self, app):
        """A completely invalid key should return 401."""
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/devices", headers={"X-API-Key": "agora_bogus_nonexistent"})
            assert resp.status_code == 401

    async def test_api_key_inherits_user_role_permissions(self, app, db_session):
        """API key should have same permissions as its owner's role."""
        viewer = await _create_user(db_session, email="keyviewer@test.com", role_name="Viewer")
        _, raw_key = await _create_db_key(db_session, viewer.id, key_type="api")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Viewer can read devices
            resp = await ac.get("/api/devices", headers={"X-API-Key": raw_key})
            assert resp.status_code == 200

            # Viewer cannot manage users
            resp = await ac.get("/api/users", headers={"X-API-Key": raw_key})
            assert resp.status_code == 403


# ── Permission intersection / ceiling ────────────────────────────────


@pytest.mark.asyncio
class TestPermissionCeiling:
    """compute_effective_permissions intersects user perms with key-type role."""

    async def test_no_ceiling_returns_user_perms(self, app, db_session):
        """Without a ceiling role configured, user gets full permissions."""
        admin = (await db_session.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        await db_session.refresh(admin, ["role"])

        perms = await compute_effective_permissions(admin, "api", db_session)
        # Admin has all permissions — should all be returned
        assert "devices:read" in perms
        assert "users:write" in perms
        assert "settings:write" in perms

    async def test_ceiling_intersects_with_user_perms(self, app, db_session):
        """When a ceiling role is set, effective perms = user ∩ ceiling."""
        admin = (await db_session.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        await db_session.refresh(admin, ["role"])

        # Set Operator as the API ceiling (Operator has no users:write, settings:write, etc.)
        operator_role = await _get_role(db_session, "Operator")
        await set_setting(db_session, SETTING_API_ROLE_ID, str(operator_role.id))
        await db_session.commit()

        perms = await compute_effective_permissions(admin, "api", db_session)
        # Intersection: admin has everything, operator has a subset
        assert "devices:read" in perms
        assert "devices:write" in perms
        # Operator doesn't have users:write, so intersection removes it
        assert "users:write" not in perms
        assert "settings:write" not in perms

    async def test_mcp_ceiling_separate_from_api(self, app, db_session):
        """MCP and API ceilings are configured independently."""
        admin = (await db_session.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        await db_session.refresh(admin, ["role"])

        # Set Viewer as MCP ceiling, leave API uncapped
        viewer_role = await _get_role(db_session, "Viewer")
        await set_setting(db_session, SETTING_MCP_ROLE_ID, str(viewer_role.id))
        await db_session.commit()

        mcp_perms = await compute_effective_permissions(admin, "mcp", db_session)
        api_perms = await compute_effective_permissions(admin, "api", db_session)

        # MCP should be capped to viewer-level
        assert "devices:read" in mcp_perms
        assert "devices:write" not in mcp_perms

        # API should be uncapped (no setting for SETTING_API_ROLE_ID)
        assert "devices:write" in api_perms

    async def test_viewer_with_ceiling_narrower_than_user(self, app, db_session):
        """A viewer user with a ceiling gets the minimum of both."""
        viewer = await _create_user(db_session, email="ceilviewer@test.com", role_name="Viewer")
        await db_session.refresh(viewer, ["role"])

        # Create a custom role with only devices:read
        custom = Role(
            name="Minimal",
            description="Very restricted",
            permissions=["devices:read"],
        )
        db_session.add(custom)
        await db_session.commit()
        await db_session.refresh(custom)

        await set_setting(db_session, SETTING_API_ROLE_ID, str(custom.id))
        await db_session.commit()

        perms = await compute_effective_permissions(viewer, "api", db_session)
        # Viewer has devices:read, groups:read, assets:read, etc.
        # Intersection with custom (only devices:read) = just devices:read
        assert perms == ["devices:read"]

    async def test_invalid_ceiling_role_id_returns_user_perms(self, app, db_session):
        """A garbled setting value should fall back to user's own perms."""
        admin = (await db_session.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        await db_session.refresh(admin, ["role"])

        await set_setting(db_session, SETTING_API_ROLE_ID, "not-a-uuid")
        await db_session.commit()

        perms = await compute_effective_permissions(admin, "api", db_session)
        assert "devices:read" in perms
        assert "users:write" in perms  # full admin perms, not capped


# ── Self-service key endpoints ───────────────────────────────────────


@pytest.mark.asyncio
class TestSelfServiceKeys:
    """Tests for /api/keys/my — self-service key management."""

    async def test_create_api_key(self, client):
        """Admin creates an API key for themselves."""
        resp = await client.post("/api/keys/my", json={"name": "My API Key", "key_type": "api"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "My API Key"
        assert data["key_type"] == "api"
        assert data["key"].startswith("agora_")
        assert "id" in data

    async def test_create_mcp_key(self, client):
        """Admin creates an MCP key."""
        resp = await client.post("/api/keys/my", json={"name": "My MCP Key", "key_type": "mcp"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["key_type"] == "mcp"

    async def test_create_key_invalid_type_rejected(self, client):
        """Invalid key_type should return 422."""
        resp = await client.post("/api/keys/my", json={"name": "Bad", "key_type": "invalid"})
        assert resp.status_code == 422

    async def test_create_key_empty_name_rejected(self, client):
        """Empty name should return 422."""
        resp = await client.post("/api/keys/my", json={"name": "   ", "key_type": "api"})
        assert resp.status_code == 422

    async def test_list_my_keys(self, client):
        """Should list only the current user's keys."""
        # Create a key
        create_resp = await client.post("/api/keys/my", json={"name": "Listed Key"})
        assert create_resp.status_code == 201
        key_id = create_resp.json()["id"]

        # List keys
        resp = await client.get("/api/keys/my")
        assert resp.status_code == 200
        keys = resp.json()
        assert any(k["id"] == key_id for k in keys)

    async def test_regenerate_my_key(self, client):
        """Regenerate should return a new secret, same name/type."""
        create_resp = await client.post("/api/keys/my", json={"name": "Regen Key"})
        key_id = create_resp.json()["id"]
        old_key = create_resp.json()["key"]
        old_prefix = create_resp.json()["key_prefix"]

        regen_resp = await client.post(f"/api/keys/my/{key_id}/regenerate")
        assert regen_resp.status_code == 200
        data = regen_resp.json()
        assert data["name"] == "Regen Key"
        assert data["key"] != old_key
        assert data["key_prefix"] != old_prefix

    async def test_delete_my_key(self, client):
        """Delete should remove the key."""
        create_resp = await client.post("/api/keys/my", json={"name": "Delete Me"})
        key_id = create_resp.json()["id"]

        del_resp = await client.delete(f"/api/keys/my/{key_id}")
        assert del_resp.status_code == 200
        assert del_resp.json()["deleted"] == key_id

        # Verify gone
        list_resp = await client.get("/api/keys/my")
        assert not any(k["id"] == key_id for k in list_resp.json())

    async def test_operator_can_create_mcp_key(self, app, db_session):
        """Operator role has mcp_keys:self, so they should be able to create MCP keys."""
        operator = await _create_user(db_session, email="op_mcp@test.com", role_name="Operator")
        op_client = await _login_as(app, "op_mcp@test.com")
        try:
            resp = await op_client.post("/api/keys/my", json={"name": "Op MCP", "key_type": "mcp"})
            assert resp.status_code == 201
            assert resp.json()["key_type"] == "mcp"
        finally:
            await op_client.aclose()

    async def test_operator_cannot_create_api_key(self, app, db_session):
        """Operator role lacks api_keys:self, so API key creation should fail."""
        operator = await _create_user(db_session, email="op_noapi@test.com", role_name="Operator")
        op_client = await _login_as(app, "op_noapi@test.com")
        try:
            resp = await op_client.post("/api/keys/my", json={"name": "Op API", "key_type": "api"})
            assert resp.status_code == 403
        finally:
            await op_client.aclose()

    async def test_viewer_cannot_create_any_key(self, app, db_session):
        """Viewer role has neither mcp_keys:self nor api_keys:self."""
        viewer = await _create_user(db_session, email="viewer_nokeys@test.com", role_name="Viewer")
        v_client = await _login_as(app, "viewer_nokeys@test.com")
        try:
            resp = await v_client.post("/api/keys/my", json={"name": "V1", "key_type": "api"})
            assert resp.status_code == 403
            resp = await v_client.post("/api/keys/my", json={"name": "V2", "key_type": "mcp"})
            assert resp.status_code == 403
        finally:
            await v_client.aclose()


# ── IDOR protection on key endpoints ─────────────────────────────────


@pytest.mark.asyncio
class TestKeyIDOR:
    """Users must not be able to touch other users' keys."""

    async def test_cannot_delete_another_users_key(self, app, db_session):
        """User A can't delete User B's key via /api/keys/my."""
        user_a = await _create_user(db_session, email="keya@test.com", role_name="Admin")
        user_b = await _create_user(db_session, email="keyb@test.com", role_name="Admin")
        key_b, _ = await _create_db_key(db_session, user_b.id, key_type="api", name="B's Key")

        a_client = await _login_as(app, "keya@test.com")
        try:
            resp = await a_client.delete(f"/api/keys/my/{key_b.id}")
            assert resp.status_code == 404  # Not found (not theirs)
        finally:
            await a_client.aclose()

    async def test_cannot_regenerate_another_users_key(self, app, db_session):
        """User A can't regenerate User B's key via /api/keys/my."""
        user_a = await _create_user(db_session, email="regena@test.com", role_name="Admin")
        user_b = await _create_user(db_session, email="regenb@test.com", role_name="Admin")
        key_b, _ = await _create_db_key(db_session, user_b.id, key_type="api", name="B Regen")

        a_client = await _login_as(app, "regena@test.com")
        try:
            resp = await a_client.post(f"/api/keys/my/{key_b.id}/regenerate")
            assert resp.status_code == 404
        finally:
            await a_client.aclose()

    async def test_list_my_keys_only_returns_own(self, app, db_session):
        """GET /api/keys/my should only return the logged-in user's keys."""
        user_a = await _create_user(db_session, email="lista@test.com", role_name="Admin")
        user_b = await _create_user(db_session, email="listb@test.com", role_name="Admin")
        await _create_db_key(db_session, user_a.id, name="A's Key")
        await _create_db_key(db_session, user_b.id, name="B's Key")

        a_client = await _login_as(app, "lista@test.com")
        try:
            resp = await a_client.get("/api/keys/my")
            assert resp.status_code == 200
            keys = resp.json()
            assert all(k["name"] != "B's Key" for k in keys)
            assert any(k["name"] == "A's Key" for k in keys)
        finally:
            await a_client.aclose()


# ── Admin key management ─────────────────────────────────────────────


@pytest.mark.asyncio
class TestAdminKeyManagement:
    """Tests for /api/keys — admin oversight endpoints."""

    async def test_admin_list_all_keys(self, client, db_session):
        """Admin should see keys from all users."""
        operator = await _create_user(db_session, email="adminlist_op@test.com",
                                      role_name="Operator")
        await _create_db_key(db_session, operator.id, name="Op Key")

        resp = await client.get("/api/keys")
        assert resp.status_code == 200
        keys = resp.json()
        assert any(k["name"] == "Op Key" for k in keys)

    async def test_admin_create_key(self, client):
        """Admin creates a key via admin endpoint."""
        resp = await client.post("/api/keys", json={"name": "Admin Created", "key_type": "api"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Admin Created"
        assert data["key"].startswith("agora_")

    async def test_admin_regenerate_others_key(self, client, db_session):
        """Admin regenerates another user's key."""
        operator = await _create_user(db_session, email="adminregen_op@test.com",
                                      role_name="Operator")
        key_row, old_raw = await _create_db_key(db_session, operator.id, name="Op Regen Key")

        resp = await client.post(f"/api/keys/{key_row.id}/regenerate")
        assert resp.status_code == 200
        data = resp.json()
        assert data["key"] != old_raw
        assert data["name"] == "Op Regen Key"

    async def test_admin_delete_others_key(self, client, db_session):
        """Admin deletes another user's key."""
        operator = await _create_user(db_session, email="admindel_op@test.com",
                                      role_name="Operator")
        key_row, _ = await _create_db_key(db_session, operator.id, name="Op Del Key")

        resp = await client.delete(f"/api/keys/{key_row.id}")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == str(key_row.id)

    async def test_admin_delete_nonexistent_key_404(self, client):
        """Deleting a non-existent key should return 404."""
        fake_id = uuid.uuid4()
        resp = await client.delete(f"/api/keys/{fake_id}")
        assert resp.status_code == 404

    async def test_non_admin_cannot_list_all_keys(self, app, db_session):
        """Operator (no api_keys:manage) should be denied admin endpoints."""
        operator = await _create_user(db_session, email="nolist_op@test.com",
                                      role_name="Operator")
        op_client = await _login_as(app, "nolist_op@test.com")
        try:
            resp = await op_client.get("/api/keys")
            assert resp.status_code == 403
        finally:
            await op_client.aclose()

    async def test_non_admin_cannot_delete_via_admin_endpoint(self, app, db_session):
        """Operator can't use admin DELETE /api/keys/{id}."""
        operator = await _create_user(db_session, email="nodel_op@test.com",
                                      role_name="Operator")
        admin = (await db_session.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        key_row, _ = await _create_db_key(db_session, admin.id, name="Admin's Key")

        op_client = await _login_as(app, "nodel_op@test.com")
        try:
            resp = await op_client.delete(f"/api/keys/{key_row.id}")
            assert resp.status_code == 403
        finally:
            await op_client.aclose()


# ── Audit logging & notifications ────────────────────────────────────


@pytest.mark.asyncio
class TestKeyAuditAndNotifications:
    """Admin key operations should produce audit entries and owner notifications."""

    async def test_admin_regenerate_creates_notification_for_owner(self, client, db_session):
        """When admin regenerates another user's key, the owner gets a notification."""
        operator = await _create_user(db_session, email="notify_op@test.com",
                                      role_name="Operator")
        key_row, _ = await _create_db_key(db_session, operator.id, name="Notify Key")

        resp = await client.post(f"/api/keys/{key_row.id}/regenerate")
        assert resp.status_code == 200

        # Check for notification
        result = await db_session.execute(
            select(Notification).where(
                Notification.user_id == operator.id,
                Notification.title == "API key regenerated",
            )
        )
        notif = result.scalar_one_or_none()
        assert notif is not None
        assert "Notify Key" in notif.message

    async def test_admin_delete_creates_notification_for_owner(self, client, db_session):
        """When admin revokes another user's key, the owner gets a notification."""
        operator = await _create_user(db_session, email="notifydel_op@test.com",
                                      role_name="Operator")
        key_row, _ = await _create_db_key(db_session, operator.id, name="Revoke Notify Key")

        resp = await client.delete(f"/api/keys/{key_row.id}")
        assert resp.status_code == 200

        result = await db_session.execute(
            select(Notification).where(
                Notification.user_id == operator.id,
                Notification.title == "API key revoked",
            )
        )
        notif = result.scalar_one_or_none()
        assert notif is not None
        assert "Revoke Notify Key" in notif.message

    async def test_admin_regen_own_key_no_notification(self, client, db_session):
        """Admin regenerating their own key should NOT create a notification."""
        admin = (await db_session.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        key_row, _ = await _create_db_key(db_session, admin.id, name="Admin Own Key")

        # Count notifications before
        before = (await db_session.execute(
            select(Notification).where(Notification.user_id == admin.id)
        )).scalars().all()
        before_count = len(before)

        resp = await client.post(f"/api/keys/{key_row.id}/regenerate")
        assert resp.status_code == 200

        # No new notification
        after = (await db_session.execute(
            select(Notification).where(Notification.user_id == admin.id)
        )).scalars().all()
        assert len(after) == before_count

    async def test_admin_regenerate_creates_audit_entry(self, client, db_session):
        """Admin regenerate should produce an audit log entry."""
        from cms.models.audit_log import AuditLog

        operator = await _create_user(db_session, email="auditop@test.com",
                                      role_name="Operator")
        key_row, _ = await _create_db_key(db_session, operator.id, name="Audit Key")

        resp = await client.post(f"/api/keys/{key_row.id}/regenerate")
        assert resp.status_code == 200

        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "api_key.regenerate",
                AuditLog.resource_id == str(key_row.id),
            )
        )
        entry = result.scalar_one_or_none()
        assert entry is not None

    async def test_admin_delete_creates_audit_entry(self, client, db_session):
        """Admin revoke should produce an audit log entry."""
        from cms.models.audit_log import AuditLog

        operator = await _create_user(db_session, email="auditdelop@test.com",
                                      role_name="Operator")
        key_row, _ = await _create_db_key(db_session, operator.id, name="Audit Del Key")

        resp = await client.delete(f"/api/keys/{key_row.id}")
        assert resp.status_code == 200

        result = await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "api_key.revoke",
                AuditLog.resource_id == str(key_row.id),
            )
        )
        entry = result.scalar_one_or_none()
        assert entry is not None


# ── Regenerated key actually works ───────────────────────────────────


@pytest.mark.asyncio
class TestRegeneratedKeyWorks:
    """After regeneration, the new key should work and the old one should not."""

    async def test_new_key_works_old_key_does_not(self, app, client, db_session):
        """Regenerated key should authenticate; old key should fail."""
        # Create key via self-service
        create_resp = await client.post("/api/keys/my", json={"name": "Cycle Key", "key_type": "api"})
        assert create_resp.status_code == 201
        old_key = create_resp.json()["key"]
        key_id = create_resp.json()["id"]

        # Regenerate
        regen_resp = await client.post(f"/api/keys/my/{key_id}/regenerate")
        assert regen_resp.status_code == 200
        new_key = regen_resp.json()["key"]

        # New key should work
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            resp = await ac.get("/api/devices", headers={"X-API-Key": new_key})
            assert resp.status_code == 200

            # Old key should fail
            resp = await ac.get("/api/devices", headers={"X-API-Key": old_key})
            assert resp.status_code == 401
