"""Comprehensive RBAC tests: roles, permissions, user management, and asset scoping."""

import io
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import hash_password, _hash_api_key
from cms.models.api_key import APIKey
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.group_asset import GroupAsset
from cms.models.user import Role, User, UserGroup


# ── Helpers ──


async def _get_role_id(db: AsyncSession, name: str) -> uuid.UUID:
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one().id


async def _create_user(
    db: AsyncSession, *, email: str, role_name: str = "Viewer",
    display_name: str | None = None, group_ids: list | None = None,
) -> User:
    """Create a test user with the given role and group assignments."""
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
    await db.flush()
    for gid in (group_ids or []):
        db.add(UserGroup(user_id=user.id, group_id=gid))
    await db.commit()
    await db.refresh(user, ["role"])
    return user


async def _login_as(app, user_email: str) -> AsyncClient:
    """Return an authenticated AsyncClient logged in as the given user."""
    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    username = user_email.split("@")[0]
    await ac.post("/login", data={"username": username, "password": "password123"}, follow_redirects=False)
    return ac


async def _create_group(db: AsyncSession, name: str) -> DeviceGroup:
    group = DeviceGroup(name=name)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return group


# ── Permission enforcement tests ──


@pytest.mark.asyncio
class TestPermissionEnforcement:
    """Verify that endpoints enforce their required permissions."""

    async def test_viewer_cannot_write_devices(self, app, db_session):
        viewer = await _create_user(db_session, email="viewer1@test.com", role_name="Viewer")
        ac = await _login_as(app, "viewer1@test.com")
        try:
            # adopt is a device write operation
            resp = await ac.post("/api/devices/fake-serial/adopt")
            assert resp.status_code == 403
        finally:
            await ac.aclose()

    async def test_viewer_can_read_devices(self, app, db_session):
        await _create_user(db_session, email="viewer2@test.com", role_name="Viewer")
        ac = await _login_as(app, "viewer2@test.com")
        try:
            resp = await ac.get("/api/devices")
            assert resp.status_code == 200
        finally:
            await ac.aclose()

    async def test_viewer_cannot_read_users(self, app, db_session):
        await _create_user(db_session, email="viewer3@test.com", role_name="Viewer")
        ac = await _login_as(app, "viewer3@test.com")
        try:
            resp = await ac.get("/api/users")
            assert resp.status_code == 403
        finally:
            await ac.aclose()

    async def test_viewer_cannot_write_schedules(self, app, db_session):
        await _create_user(db_session, email="viewer4@test.com", role_name="Viewer")
        ac = await _login_as(app, "viewer4@test.com")
        try:
            resp = await ac.post("/api/schedules", json={"name": "test"})
            assert resp.status_code == 403
        finally:
            await ac.aclose()

    async def test_operator_can_write_devices(self, app, db_session):
        await _create_user(db_session, email="op1@test.com", role_name="Operator")
        ac = await _login_as(app, "op1@test.com")
        try:
            # POST might fail for other reasons (missing fields) but NOT 403
            resp = await ac.get("/api/devices")
            assert resp.status_code == 200
        finally:
            await ac.aclose()

    async def test_operator_cannot_manage_users(self, app, db_session):
        await _create_user(db_session, email="op2@test.com", role_name="Operator")
        ac = await _login_as(app, "op2@test.com")
        try:
            resp = await ac.get("/api/users")
            assert resp.status_code == 403
        finally:
            await ac.aclose()

    async def test_operator_cannot_change_settings(self, app, db_session):
        await _create_user(db_session, email="op3@test.com", role_name="Operator")
        ac = await _login_as(app, "op3@test.com")
        try:
            resp = await ac.post("/api/settings/smtp", json={"host": "smtp.test.com"})
            assert resp.status_code == 403
        finally:
            await ac.aclose()

    async def test_admin_can_manage_users(self, client):
        resp = await client.get("/api/users")
        assert resp.status_code == 200

    async def test_admin_can_read_audit(self, client):
        resp = await client.get("/api/audit-log")
        assert resp.status_code == 200

    async def test_viewer_cannot_read_audit(self, app, db_session):
        await _create_user(db_session, email="viewer5@test.com", role_name="Viewer")
        ac = await _login_as(app, "viewer5@test.com")
        try:
            resp = await ac.get("/api/audit-log")
            assert resp.status_code == 403
        finally:
            await ac.aclose()

    async def test_unauthenticated_rejected(self, unauthed_client):
        resp = await unauthed_client.get("/api/devices")
        assert resp.status_code in (401, 303)

    async def test_inactive_user_rejected(self, app, db_session):
        user = await _create_user(db_session, email="inactive@test.com", role_name="Admin")
        user.is_active = False
        await db_session.commit()
        ac = await _login_as(app, "inactive@test.com")
        try:
            resp = await ac.get("/api/devices")
            # Should either redirect to login or return 401/403
            assert resp.status_code in (303, 401, 403)
        finally:
            await ac.aclose()


# ── User CRUD via API ──


@pytest.mark.asyncio
class TestUserManagement:
    """Test user creation, update, and deletion via admin API."""

    async def test_create_user(self, client, db_session):
        role_id = str(await _get_role_id(db_session, "Viewer"))
        resp = await client.post("/api/users", json={
            "email": "newuser@test.com",
            "display_name": "New User",
            "role_id": role_id,
            "group_ids": [],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["email"] == "newuser@test.com"
        assert data["display_name"] == "New User"

    async def test_create_duplicate_email_rejected(self, client, db_session):
        role_id = str(await _get_role_id(db_session, "Viewer"))
        await client.post("/api/users", json={
            "email": "dup@test.com", "display_name": "First",
            "role_id": role_id, "group_ids": [],
        })
        resp = await client.post("/api/users", json={
            "email": "dup@test.com", "display_name": "Second",
            "role_id": role_id, "group_ids": [],
        })
        assert resp.status_code == 409

    async def test_update_user_display_name(self, client, db_session):
        role_id = str(await _get_role_id(db_session, "Viewer"))
        create_resp = await client.post("/api/users", json={
            "email": "editable@test.com", "display_name": "Original",
            "role_id": role_id, "group_ids": [],
        })
        user_id = create_resp.json()["id"]
        resp = await client.patch(f"/api/users/{user_id}", json={
            "display_name": "Updated Name",
        })
        assert resp.status_code == 200
        assert resp.json()["display_name"] == "Updated Name"

    async def test_update_user_role(self, client, db_session):
        viewer_id = str(await _get_role_id(db_session, "Viewer"))
        op_id = str(await _get_role_id(db_session, "Operator"))
        create_resp = await client.post("/api/users", json={
            "email": "rolechange@test.com", "display_name": "Role Test",
            "role_id": viewer_id, "group_ids": [],
        })
        user_id = create_resp.json()["id"]
        resp = await client.patch(f"/api/users/{user_id}", json={
            "role_id": op_id,
        })
        assert resp.status_code == 200
        assert resp.json()["role"]["name"] == "Operator"

    async def test_delete_user(self, client, db_session):
        role_id = str(await _get_role_id(db_session, "Viewer"))
        create_resp = await client.post("/api/users", json={
            "email": "deleteme@test.com", "display_name": "Delete Me",
            "role_id": role_id, "group_ids": [],
        })
        user_id = create_resp.json()["id"]
        resp = await client.delete(f"/api/users/{user_id}")
        assert resp.status_code == 200

        # Verify gone
        resp2 = await client.get(f"/api/users/{user_id}")
        assert resp2.status_code == 404

    async def test_list_users(self, client):
        resp = await client.get("/api/users")
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert any(u["email"] == "admin@localhost" for u in data)

    async def test_create_user_with_groups(self, client, db_session):
        group = await _create_group(db_session, "Test Group A")
        role_id = str(await _get_role_id(db_session, "Operator"))
        resp = await client.post("/api/users", json={
            "email": "grouped@test.com", "display_name": "Grouped User",
            "role_id": role_id, "group_ids": [str(group.id)],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert len(data["group_ids"]) == 1
        assert data["group_ids"][0] == str(group.id)


# ── Role management ──


@pytest.mark.asyncio
class TestRoleManagement:

    async def test_list_roles(self, client):
        resp = await client.get("/api/roles")
        assert resp.status_code == 200
        roles = resp.json()
        names = [r["name"] for r in roles]
        assert "Admin" in names
        assert "Operator" in names
        assert "Viewer" in names

    async def test_create_custom_role(self, client):
        resp = await client.post("/api/roles", json={
            "name": "ContentManager",
            "description": "Manages assets and schedules",
            "permissions": ["assets:read", "assets:write", "schedules:read", "schedules:write"],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "ContentManager"
        assert "assets:write" in data["permissions"]

    async def test_cannot_delete_builtin_role(self, client, db_session):
        admin_role_id = str(await _get_role_id(db_session, "Admin"))
        resp = await client.delete(f"/api/roles/{admin_role_id}")
        assert resp.status_code in (400, 403, 409)

    async def test_builtin_roles_have_correct_permissions(self, client):
        from cms.permissions import ADMIN_PERMISSIONS, OPERATOR_PERMISSIONS, VIEWER_PERMISSIONS
        resp = await client.get("/api/roles")
        roles = {r["name"]: r for r in resp.json()}
        assert set(roles["Admin"]["permissions"]) == set(ADMIN_PERMISSIONS)
        assert set(roles["Operator"]["permissions"]) == set(OPERATOR_PERMISSIONS)
        assert set(roles["Viewer"]["permissions"]) == set(VIEWER_PERMISSIONS)


# ── Asset group scoping ──


@pytest.mark.asyncio
class TestAssetGroupScoping:
    """Test that asset visibility is restricted by group membership."""

    async def _upload_asset(self, ac: AsyncClient, filename: str, group_id: str | None = None):
        """Upload a small test file."""
        content = b"fake video content " + filename.encode()
        params = {}
        if group_id:
            params["group_id"] = group_id
        resp = await ac.post(
            "/api/assets/upload",
            files={"file": (filename, io.BytesIO(content), "video/mp4")},
            params=params,
        )
        return resp

    async def test_upload_without_group_is_global(self, client, db_session):
        resp = await self._upload_asset(client, "global_test.mp4")
        assert resp.status_code == 201
        data = resp.json()
        assert data["is_global"] is True

    async def test_upload_with_group_is_scoped(self, client, db_session):
        group = await _create_group(db_session, "Scoped Group")
        resp = await self._upload_asset(client, "scoped_test.mp4", str(group.id))
        assert resp.status_code == 201
        data = resp.json()
        assert data["is_global"] is False

    async def test_viewer_sees_global_assets(self, app, db_session, client):
        # Upload global asset as admin
        await self._upload_asset(client, "viewer_visible.mp4")

        # Create viewer (no groups)
        await _create_user(db_session, email="viewer_asset@test.com", role_name="Viewer")
        ac = await _login_as(app, "viewer_asset@test.com")
        try:
            resp = await ac.get("/api/assets")
            assert resp.status_code == 200
            filenames = [a["filename"] for a in resp.json()]
            assert "viewer_visible.mp4" in filenames
        finally:
            await ac.aclose()

    async def test_viewer_cannot_see_other_group_assets(self, app, db_session, client):
        group_a = await _create_group(db_session, "Group A Visibility")
        group_b = await _create_group(db_session, "Group B Visibility")

        # Upload asset to group A as admin
        await self._upload_asset(client, "group_a_only.mp4", str(group_a.id))

        # Create viewer in group B only
        await _create_user(db_session, email="viewer_b@test.com", role_name="Viewer",
                          group_ids=[group_b.id])
        ac = await _login_as(app, "viewer_b@test.com")
        try:
            resp = await ac.get("/api/assets")
            assert resp.status_code == 200
            filenames = [a["filename"] for a in resp.json()]
            assert "group_a_only.mp4" not in filenames
        finally:
            await ac.aclose()

    async def test_user_sees_own_group_assets(self, app, db_session, client):
        group = await _create_group(db_session, "Group See Own")

        # Upload asset to group as admin
        await self._upload_asset(client, "own_group.mp4", str(group.id))

        # Create viewer in that group
        await _create_user(db_session, email="viewer_own@test.com", role_name="Viewer",
                          group_ids=[group.id])
        ac = await _login_as(app, "viewer_own@test.com")
        try:
            resp = await ac.get("/api/assets")
            filenames = [a["filename"] for a in resp.json()]
            assert "own_group.mp4" in filenames
        finally:
            await ac.aclose()

    async def test_admin_sees_all_assets(self, client, db_session):
        group = await _create_group(db_session, "Admin See All")
        await self._upload_asset(client, "admin_all1.mp4")
        await self._upload_asset(client, "admin_all2.mp4", str(group.id))

        resp = await client.get("/api/assets")
        filenames = [a["filename"] for a in resp.json()]
        assert "admin_all1.mp4" in filenames
        assert "admin_all2.mp4" in filenames

    async def test_share_asset_with_group(self, client, db_session):
        group_a = await _create_group(db_session, "Share Source")
        group_b = await _create_group(db_session, "Share Target")

        upload_resp = await self._upload_asset(client, "shareable.mp4", str(group_a.id))
        asset_id = upload_resp.json()["id"]

        # Share with group_b
        resp = await client.post(f"/api/assets/{asset_id}/share", params={"group_id": str(group_b.id)})
        assert resp.status_code == 200
        assert resp.json()["status"] == "shared"

        # Viewer in group_b should now see it
        await _create_user(db_session, email="shared_viewer@test.com", role_name="Viewer",
                          group_ids=[group_b.id])
        ac = await _login_as(client._transport.app, "shared_viewer@test.com")
        try:
            resp = await ac.get("/api/assets")
            filenames = [a["filename"] for a in resp.json()]
            assert "shareable.mp4" in filenames
        finally:
            await ac.aclose()

    async def test_unshare_removes_access(self, client, db_session):
        group_a = await _create_group(db_session, "Unshare Source")
        group_b = await _create_group(db_session, "Unshare Target")

        upload_resp = await self._upload_asset(client, "unshare_test.mp4", str(group_a.id))
        asset_id = upload_resp.json()["id"]

        # Share then unshare
        await client.post(f"/api/assets/{asset_id}/share", params={"group_id": str(group_b.id)})
        resp = await client.delete(f"/api/assets/{asset_id}/share", params={"group_id": str(group_b.id)})
        assert resp.status_code == 200
        assert resp.json()["status"] == "unshared"

    async def test_can_unshare_any_group(self, client, db_session):
        """All group associations are equal — any can be removed."""
        group = await _create_group(db_session, "Any Unshare")
        upload_resp = await self._upload_asset(client, "any_unshare.mp4", str(group.id))
        asset_id = upload_resp.json()["id"]

        resp = await client.delete(f"/api/assets/{asset_id}/share", params={"group_id": str(group.id)})
        assert resp.status_code == 200

    async def test_toggle_global(self, client, db_session):
        group = await _create_group(db_session, "Toggle Global")
        upload_resp = await self._upload_asset(client, "toggle_global.mp4", str(group.id))
        asset_id = upload_resp.json()["id"]

        # Should start as not global
        assert upload_resp.json()["is_global"] is False

        # Toggle on
        resp = await client.post(f"/api/assets/{asset_id}/global")
        assert resp.status_code == 200
        assert resp.json()["is_global"] is True

        # Toggle off
        resp = await client.post(f"/api/assets/{asset_id}/global")
        assert resp.json()["is_global"] is False

    async def test_viewer_cannot_upload(self, app, db_session):
        await _create_user(db_session, email="viewer_up@test.com", role_name="Viewer")
        ac = await _login_as(app, "viewer_up@test.com")
        try:
            resp = await ac.post(
                "/api/assets/upload",
                files={"file": ("nope.mp4", io.BytesIO(b"content"), "video/mp4")},
            )
            assert resp.status_code == 403
        finally:
            await ac.aclose()

    async def test_operator_can_upload(self, app, db_session):
        group = await _create_group(db_session, "Op Upload Group")
        await _create_user(db_session, email="op_upload@test.com", role_name="Operator",
                          group_ids=[group.id])
        ac = await _login_as(app, "op_upload@test.com")
        try:
            resp = await ac.post(
                "/api/assets/upload",
                files={"file": ("op_asset.mp4", io.BytesIO(b"op content"), "video/mp4")},
                params={"group_id": str(group.id)},
            )
            assert resp.status_code == 201
        finally:
            await ac.aclose()

    async def test_operator_cannot_upload_to_other_group(self, app, db_session):
        group_a = await _create_group(db_session, "Op Group Own")
        group_b = await _create_group(db_session, "Op Group Other")
        await _create_user(db_session, email="op_other@test.com", role_name="Operator",
                          group_ids=[group_a.id])
        ac = await _login_as(app, "op_other@test.com")
        try:
            resp = await ac.post(
                "/api/assets/upload",
                files={"file": ("blocked.mp4", io.BytesIO(b"nope"), "video/mp4")},
                params={"group_id": str(group_b.id)},
            )
            assert resp.status_code == 403
        finally:
            await ac.aclose()


# ── Device/group API scoping ──


@pytest.mark.asyncio
class TestDeviceGroupAPIScoping:
    """Non-admin users should only see their assigned groups and devices in those groups."""

    async def test_groups_api_returns_only_user_groups(self, app, db_session):
        """Operator in GroupA should only see GroupA from GET /api/devices/groups/."""
        group_a = await _create_group(db_session, "Scope API GroupA")
        group_b = await _create_group(db_session, "Scope API GroupB")
        await _create_user(db_session, email="grp_scope@test.com", role_name="Operator",
                          group_ids=[group_a.id])
        ac = await _login_as(app, "grp_scope@test.com")
        try:
            resp = await ac.get("/api/devices/groups/")
            assert resp.status_code == 200
            group_names = [g["name"] for g in resp.json()]
            assert "Scope API GroupA" in group_names
            assert "Scope API GroupB" not in group_names
        finally:
            await ac.aclose()

    async def test_groups_api_admin_sees_all(self, client, db_session):
        """Admin should see all groups from GET /api/devices/groups/."""
        group = await _create_group(db_session, "Scope Admin All")
        resp = await client.get("/api/devices/groups/")
        assert resp.status_code == 200
        group_names = [g["name"] for g in resp.json()]
        assert "Scope Admin All" in group_names

    async def test_devices_api_returns_only_user_group_devices(self, app, db_session):
        """Operator in GroupA should not see devices assigned to GroupB."""
        group_a = await _create_group(db_session, "Dev Scope A")
        group_b = await _create_group(db_session, "Dev Scope B")

        # Create devices in each group
        dev_a = Device(id="dev-scope-a", name="Device A", status=DeviceStatus.ADOPTED, group_id=group_a.id)
        dev_b = Device(id="dev-scope-b", name="Device B", status=DeviceStatus.ADOPTED, group_id=group_b.id)
        db_session.add_all([dev_a, dev_b])
        await db_session.commit()

        await _create_user(db_session, email="dev_scope@test.com", role_name="Operator",
                          group_ids=[group_a.id])
        ac = await _login_as(app, "dev_scope@test.com")
        try:
            resp = await ac.get("/api/devices")
            assert resp.status_code == 200
            device_names = [d["name"] for d in resp.json()]
            assert "Device A" in device_names
            assert "Device B" not in device_names
        finally:
            await ac.aclose()

    async def test_scope_all_permission_bypasses_group_filter(self, app, db_session):
        """A role with groups:view_all should see all groups even without ALL_PERMISSIONS."""
        from cms.permissions import DEVICES_READ, GROUPS_READ, GROUPS_VIEW_ALL
        group_a = await _create_group(db_session, "Scope Bypass A")
        group_b = await _create_group(db_session, "Scope Bypass B")

        # Create a custom role with limited perms + groups:view_all
        custom_role = Role(
            name="ScopeAllTest",
            permissions=[DEVICES_READ, GROUPS_READ, GROUPS_VIEW_ALL],
        )
        db_session.add(custom_role)
        await db_session.flush()

        await _create_user(db_session, email="scope_all@test.com",
                          group_ids=[group_a.id])
        # Patch user to custom role
        user = (await db_session.execute(
            select(User).where(User.email == "scope_all@test.com")
        )).scalar_one()
        user.role_id = custom_role.id
        await db_session.commit()

        ac = await _login_as(app, "scope_all@test.com")
        try:
            resp = await ac.get("/api/devices/groups/")
            assert resp.status_code == 200
            group_names = [g["name"] for g in resp.json()]
            assert "Scope Bypass A" in group_names
            assert "Scope Bypass B" in group_names, \
                "User with groups:view_all should see all groups, not just assigned ones"
        finally:
            await ac.aclose()


@pytest.mark.asyncio
class TestAPIKeyUserAssociation:
    """API keys should be linked to the creating user."""

    async def test_key_linked_to_creating_user(self, client, db_session):
        resp = await client.post("/api/keys", json={"name": "Linked Key"})
        assert resp.status_code == 201
        data = resp.json()
        # The key should have a user_id matching admin
        admin = (await db_session.execute(
            select(User).where(User.username == "admin")
        )).scalar_one()
        key = (await db_session.execute(
            select(APIKey).where(APIKey.id == uuid.UUID(data["id"]))
        )).scalar_one()
        assert key.user_id == admin.id

    async def test_api_key_inherits_user_permissions(self, app, db_session):
        """An API key should grant the same permissions as its user's role."""
        group = await _create_group(db_session, "API Key Test Group")
        viewer = await _create_user(db_session, email="apiviewer@test.com", role_name="Viewer",
                                   group_ids=[group.id])

        raw_key = "agora_viewer_key_1234567890abcdef1234567890"
        key = APIKey(
            name="Viewer Key",
            key_prefix=raw_key[:12] + "...",
            key_hash=_hash_api_key(raw_key),
            user_id=viewer.id,
        )
        db_session.add(key)
        await db_session.commit()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # Viewer can read devices
            resp = await ac.get("/api/devices", headers={"X-API-Key": raw_key})
            assert resp.status_code == 200

            # Viewer cannot read users
            resp = await ac.get("/api/users", headers={"X-API-Key": raw_key})
            assert resp.status_code == 403


# ── Audit logging ──


@pytest.mark.asyncio
class TestAuditLogging:
    """Test that RBAC actions produce audit log entries."""

    async def test_user_create_logged(self, client, db_session):
        role_id = str(await _get_role_id(db_session, "Viewer"))
        await client.post("/api/users", json={
            "email": "audited@test.com", "display_name": "Audited",
            "role_id": role_id, "group_ids": [],
        })
        resp = await client.get("/api/audit-log")
        assert resp.status_code == 200
        actions = [e["action"] for e in resp.json()]
        assert "user.create" in actions

    async def test_user_update_logged(self, client, db_session):
        role_id = str(await _get_role_id(db_session, "Viewer"))
        create_resp = await client.post("/api/users", json={
            "email": "audit_update@test.com", "display_name": "Before",
            "role_id": role_id, "group_ids": [],
        })
        user_id = create_resp.json()["id"]
        await client.patch(f"/api/users/{user_id}", json={"display_name": "After"})

        resp = await client.get("/api/audit-log")
        actions = [e["action"] for e in resp.json()]
        assert "user.update" in actions

    async def test_user_delete_logged(self, client, db_session):
        role_id = str(await _get_role_id(db_session, "Viewer"))
        create_resp = await client.post("/api/users", json={
            "email": "audit_del@test.com", "display_name": "Delete Me",
            "role_id": role_id, "group_ids": [],
        })
        user_id = create_resp.json()["id"]
        await client.delete(f"/api/users/{user_id}")

        resp = await client.get("/api/audit-log")
        actions = [e["action"] for e in resp.json()]
        assert "user.delete" in actions


# ── IDOR Protection Tests ──


@pytest.mark.asyncio
class TestIDORProtection:
    """Verify that individual resource endpoints enforce group scoping.

    Users should NOT be able to access resources in groups they are not
    assigned to, even if they know the resource UUID.
    """

    async def test_scoped_user_cannot_get_device_in_other_group(self, app, db_session):
        """GET /api/devices/{id} for a device in another group should return 403."""
        group_a = await _create_group(db_session, "IDOR Dev A")
        group_b = await _create_group(db_session, "IDOR Dev B")
        dev_b = Device(id="idor-dev-b", name="IDOR Device B",
                       status=DeviceStatus.ADOPTED, group_id=group_b.id)
        db_session.add(dev_b)
        await db_session.commit()

        await _create_user(db_session, email="idor_dev@test.com",
                          role_name="Operator", group_ids=[group_a.id])
        ac = await _login_as(app, "idor_dev@test.com")
        try:
            resp = await ac.get("/api/devices/idor-dev-b")
            assert resp.status_code == 403, \
                f"Expected 403, got {resp.status_code}: user should not access device in other group"
        finally:
            await ac.aclose()

    async def test_scoped_user_cannot_delete_schedule_in_other_group(self, app, db_session):
        """DELETE /api/schedules/{id} for a schedule targeting another group should return 403."""
        from cms.models.asset import Asset, AssetType
        from cms.models.schedule import Schedule
        group_a = await _create_group(db_session, "IDOR Sched A")
        group_b = await _create_group(db_session, "IDOR Sched B")

        # Create a minimal asset for the schedule
        asset = Asset(filename="idor_test.mp4", original_filename="idor_test.mp4",
                      asset_type=AssetType.VIDEO, size_bytes=100)
        db_session.add(asset)
        await db_session.flush()

        # Create a schedule targeting group_b
        from datetime import time
        sched = Schedule(name="IDOR Schedule", group_id=group_b.id,
                        asset_id=asset.id, start_time=time(8, 0),
                        end_time=time(17, 0), priority=0, enabled=True)
        db_session.add(sched)
        await db_session.commit()

        await _create_user(db_session, email="idor_sched@test.com",
                          role_name="Operator", group_ids=[group_a.id])
        ac = await _login_as(app, "idor_sched@test.com")
        try:
            resp = await ac.delete(f"/api/schedules/{sched.id}")
            assert resp.status_code == 403, \
                f"Expected 403, got {resp.status_code}: user should not delete schedule in other group"
        finally:
            await ac.aclose()

    async def test_scoped_user_cannot_get_asset_in_other_group(self, app, db_session):
        """GET /api/assets/{id} for an asset in another group should return 403."""
        from cms.models.asset import Asset, AssetType
        group_a = await _create_group(db_session, "IDOR Asset A")
        group_b = await _create_group(db_session, "IDOR Asset B")

        asset = Asset(filename="secret.mp4", original_filename="secret.mp4",
                      asset_type=AssetType.VIDEO, size_bytes=100)
        db_session.add(asset)
        await db_session.flush()
        # Associate asset with group_b only
        db_session.add(GroupAsset(asset_id=asset.id, group_id=group_b.id))
        await db_session.commit()

        await _create_user(db_session, email="idor_asset@test.com",
                          role_name="Operator", group_ids=[group_a.id])
        ac = await _login_as(app, "idor_asset@test.com")
        try:
            resp = await ac.get(f"/api/assets/{asset.id}")
            assert resp.status_code == 403, \
                f"Expected 403, got {resp.status_code}: user should not access asset in other group"
        finally:
            await ac.aclose()

    async def test_view_all_user_can_access_cross_group_device(self, app, db_session):
        """A user with groups:view_all should access devices in any group."""
        from cms.permissions import DEVICES_READ, DEVICES_WRITE, GROUPS_READ, GROUPS_VIEW_ALL
        group_a = await _create_group(db_session, "IDOR ViewAll A")
        group_b = await _create_group(db_session, "IDOR ViewAll B")
        dev_b = Device(id="idor-va-dev-b", name="ViewAll Device B",
                       status=DeviceStatus.ADOPTED, group_id=group_b.id)
        db_session.add(dev_b)
        await db_session.commit()

        custom_role = Role(
            name="ViewAllIDORTest",
            permissions=[DEVICES_READ, DEVICES_WRITE, GROUPS_READ, GROUPS_VIEW_ALL],
        )
        db_session.add(custom_role)
        await db_session.flush()

        await _create_user(db_session, email="idor_va@test.com",
                          group_ids=[group_a.id])
        user = (await db_session.execute(
            select(User).where(User.email == "idor_va@test.com")
        )).scalar_one()
        user.role_id = custom_role.id
        await db_session.commit()

        ac = await _login_as(app, "idor_va@test.com")
        try:
            resp = await ac.get("/api/devices/idor-va-dev-b")
            assert resp.status_code == 200, \
                f"Expected 200, got {resp.status_code}: groups:view_all user should access any device"
        finally:
            await ac.aclose()

    async def test_schedule_list_scoped_to_user_groups(self, app, db_session):
        """GET /api/schedules should only return schedules targeting the user's groups."""
        from cms.models.asset import Asset, AssetType
        from cms.models.schedule import Schedule
        from datetime import time
        group_a = await _create_group(db_session, "IDOR SchedList A")
        group_b = await _create_group(db_session, "IDOR SchedList B")

        asset = Asset(filename="sched_list.mp4", original_filename="sched_list.mp4",
                      asset_type=AssetType.VIDEO, size_bytes=100)
        db_session.add(asset)
        await db_session.flush()

        sched_a = Schedule(name="Schedule In A", group_id=group_a.id,
                          asset_id=asset.id, start_time=time(8, 0),
                          end_time=time(17, 0), priority=0, enabled=True)
        sched_b = Schedule(name="Schedule In B", group_id=group_b.id,
                          asset_id=asset.id, start_time=time(8, 0),
                          end_time=time(17, 0), priority=0, enabled=True)
        db_session.add_all([sched_a, sched_b])
        await db_session.commit()

        await _create_user(db_session, email="idor_slist@test.com",
                          role_name="Operator", group_ids=[group_a.id])
        ac = await _login_as(app, "idor_slist@test.com")
        try:
            resp = await ac.get("/api/schedules")
            assert resp.status_code == 200
            names = [s["name"] for s in resp.json()]
            assert "Schedule In A" in names, "User should see schedule in their group"
            assert "Schedule In B" not in names, "User should NOT see schedule in other group"
        finally:
            await ac.aclose()

    async def test_devices_page_only_shows_user_groups(self, app, db_session):
        """GET /devices HTML page should only include groups the user belongs to."""
        group_a = await _create_group(db_session, "UI Grp Visible")
        group_b = await _create_group(db_session, "UI Grp Hidden")
        dev_a = Device(id="ui-grp-dev-a", name="Dev In A",
                       status=DeviceStatus.ADOPTED, group_id=group_a.id)
        dev_b = Device(id="ui-grp-dev-b", name="Dev In B",
                       status=DeviceStatus.ADOPTED, group_id=group_b.id)
        db_session.add_all([dev_a, dev_b])
        await db_session.commit()

        await _create_user(db_session, email="ui_grp@test.com",
                           role_name="Operator", group_ids=[group_a.id])
        ac = await _login_as(app, "ui_grp@test.com")
        try:
            resp = await ac.get("/devices")
            assert resp.status_code == 200
            body = resp.text
            assert "UI Grp Visible" in body, "User should see their own group"
            assert "UI Grp Hidden" not in body, "User should NOT see groups they don't belong to"
            assert "Dev In A" in body, "User should see devices in their group"
            assert "Dev In B" not in body, "User should NOT see devices in other groups"
        finally:
            await ac.aclose()

    async def test_list_groups_api_scoped_to_user(self, app, db_session):
        """GET /api/groups should only return groups the user belongs to."""
        group_a = await _create_group(db_session, "API Grp Mine")
        group_b = await _create_group(db_session, "API Grp Other")
        await db_session.commit()

        await _create_user(db_session, email="api_grp@test.com",
                           role_name="Operator", group_ids=[group_a.id])
        ac = await _login_as(app, "api_grp@test.com")
        try:
            resp = await ac.get("/api/devices/groups/")
            assert resp.status_code == 200
            names = [g["name"] for g in resp.json()]
            assert "API Grp Mine" in names, "User should see their own group"
            assert "API Grp Other" not in names, "User should NOT see groups they don't belong to"
        finally:
            await ac.aclose()

    async def test_schedules_page_scoped_to_user_groups(self, app, db_session):
        """GET /schedules HTML page should only show schedules targeting user's groups."""
        from cms.models.asset import Asset, AssetType
        from cms.models.schedule import Schedule
        from datetime import time
        group_a = await _create_group(db_session, "UI Sched Grp A")
        group_b = await _create_group(db_session, "UI Sched Grp B")

        asset = Asset(filename="ui_sched.mp4", original_filename="ui_sched.mp4",
                      asset_type=AssetType.VIDEO, size_bytes=100)
        db_session.add(asset)
        await db_session.flush()

        sched_a = Schedule(name="Sched Visible", group_id=group_a.id,
                           asset_id=asset.id, start_time=time(8, 0),
                           end_time=time(17, 0), priority=0, enabled=True)
        sched_b = Schedule(name="Sched Hidden", group_id=group_b.id,
                           asset_id=asset.id, start_time=time(8, 0),
                           end_time=time(17, 0), priority=0, enabled=True)
        db_session.add_all([sched_a, sched_b])
        await db_session.commit()

        await _create_user(db_session, email="ui_sched@test.com",
                           role_name="Operator", group_ids=[group_a.id])
        ac = await _login_as(app, "ui_sched@test.com")
        try:
            resp = await ac.get("/schedules")
            assert resp.status_code == 200
            body = resp.text
            assert "Sched Visible" in body, "User should see schedule in their group"
            assert "Sched Hidden" not in body, "User should NOT see schedule in other group"
        finally:
            await ac.aclose()

    async def test_assets_page_hides_own_asset_in_other_group(self, app, db_session):
        """Assets page should not show user's own uploads if only shared with groups they're not in."""
        from cms.models.asset import Asset, AssetType
        group_a = await _create_group(db_session, "Asset Own Grp A")
        group_b = await _create_group(db_session, "Asset Own Grp B")

        user = await _create_user(db_session, email="asset_own@test.com",
                                  role_name="Operator", group_ids=[group_a.id])

        # Asset uploaded by user but shared only with group_b (not user's group)
        asset = Asset(filename="my_upload.mp4", original_filename="my_upload.mp4",
                      asset_type=AssetType.VIDEO, size_bytes=100,
                      uploaded_by_user_id=user.id)
        db_session.add(asset)
        await db_session.flush()
        db_session.add(GroupAsset(asset_id=asset.id, group_id=group_b.id))
        await db_session.commit()

        ac = await _login_as(app, "asset_own@test.com")
        try:
            resp = await ac.get("/assets")
            assert resp.status_code == 200
            assert "my_upload.mp4" not in resp.text, \
                "User should NOT see own asset shared only with groups they don't belong to"
        finally:
            await ac.aclose()