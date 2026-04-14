"""Tests for group permission fixes.

Ensures:
- Operators have groups:write (can manage groups and splash screens)
- Device Groups section is gated on groups:read
- Ungrouped Devices section is gated on groups:read
- Read-only splash display shows 'No default asset' for viewers
"""

import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from cms.permissions import (
    GROUPS_WRITE,
    GROUPS_READ,
    OPERATOR_PERMISSIONS,
    VIEWER_PERMISSIONS,
    ADMIN_PERMISSIONS,
)


# ── Unit tests for permission model ──


class TestGroupPermissions:
    """Verify groups:write is in operator permissions."""

    def test_operator_has_groups_write(self):
        assert GROUPS_WRITE in OPERATOR_PERMISSIONS

    def test_operator_has_groups_read(self):
        assert GROUPS_READ in OPERATOR_PERMISSIONS

    def test_admin_has_groups_write(self):
        assert GROUPS_WRITE in ADMIN_PERMISSIONS

    def test_viewer_no_groups_write(self):
        assert GROUPS_WRITE not in VIEWER_PERMISSIONS

    def test_viewer_has_groups_read(self):
        assert GROUPS_READ in VIEWER_PERMISSIONS


# ── Helpers ──


async def _create_user(app, *, username, role_name, group_ids=None):
    """Create a user with the given role and return a logged-in client.

    If group_ids is provided, assign the user to those groups.
    """
    from sqlalchemy import select
    from cms.database import get_db
    from cms.models.user import Role, User, UserGroup
    from cms.auth import hash_password

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        result = await db.execute(select(Role).where(Role.name == role_name))
        role = result.scalar_one()
        user = User(
            username=username,
            email=f"{username}@test.com",
            display_name=f"Test {role_name}",
            password_hash=hash_password("testpass"),
            role_id=role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(user)
        await db.flush()
        if group_ids:
            for gid in group_ids:
                db.add(UserGroup(user_id=user.id, group_id=uuid.UUID(gid) if isinstance(gid, str) else gid))
        await db.commit()
        break

    transport = ASGITransport(app=app)
    client = AsyncClient(transport=transport, base_url="http://test")
    await client.post("/login", data={"username": username, "password": "testpass"}, follow_redirects=False)
    return client


async def _create_group(app, *, name="Test Group"):
    """Create a device group via the admin client and return the group ID."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/login", data={"username": "admin", "password": "testpass"}, follow_redirects=False)
        resp = await ac.post("/api/devices/groups/", json={"name": name})
        return resp.json()["id"]


# ── API tests ──


@pytest.mark.asyncio
class TestGroupAPIPermissions:
    """Verify operators can manage groups via API."""

    async def test_operator_can_create_group(self, app):
        client = await _create_user(app, username="op_grp1", role_name="Operator")
        try:
            resp = await client.post("/api/devices/groups/", json={"name": "Operator Group"})
            assert resp.status_code == 201
        finally:
            await client.aclose()

    async def test_operator_can_delete_group(self, app):
        group_id = await _create_group(app, name="To Delete")
        client = await _create_user(app, username="op_grp2", role_name="Operator", group_ids=[group_id])
        try:
            resp = await client.delete(f"/api/devices/groups/{group_id}")
            assert resp.status_code in (200, 204)
        finally:
            await client.aclose()

    async def test_operator_can_update_group(self, app):
        group_id = await _create_group(app, name="To Update")
        client = await _create_user(app, username="op_grp3", role_name="Operator", group_ids=[group_id])
        try:
            resp = await client.patch(f"/api/devices/groups/{group_id}", json={"name": "Updated"})
            assert resp.status_code == 200
        finally:
            await client.aclose()

    async def test_viewer_cannot_create_group(self, app):
        client = await _create_user(app, username="vw_grp1", role_name="Viewer")
        try:
            resp = await client.post("/api/devices/groups/", json={"name": "Should Fail"})
            assert resp.status_code == 403
        finally:
            await client.aclose()

    async def test_viewer_cannot_delete_group(self, app):
        group_id = await _create_group(app, name="Viewer No Delete")
        client = await _create_user(app, username="vw_grp2", role_name="Viewer", group_ids=[group_id])
        try:
            resp = await client.delete(f"/api/devices/groups/{group_id}")
            assert resp.status_code == 403
        finally:
            await client.aclose()


# ── UI visibility tests ──


@pytest.mark.asyncio
class TestGroupUIVisibility:
    """Verify Device Groups and Ungrouped sections are gated on groups:read."""

    async def test_admin_sees_device_groups_section(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            await ac.post("/login", data={"username": "admin", "password": "testpass"}, follow_redirects=False)
            resp = await ac.get("/devices")
            assert resp.status_code == 200
            assert "Device Groups" in resp.text

    async def test_operator_sees_device_groups_section(self, app):
        client = await _create_user(app, username="op_ui1", role_name="Operator")
        try:
            resp = await client.get("/devices")
            assert resp.status_code == 200
            assert "Device Groups" in resp.text
        finally:
            await client.aclose()

    async def test_viewer_sees_device_groups_section(self, app):
        """Viewers have groups:read so they should see the section."""
        client = await _create_user(app, username="vw_ui1", role_name="Viewer")
        try:
            resp = await client.get("/devices")
            assert resp.status_code == 200
            assert "Device Groups" in resp.text
        finally:
            await client.aclose()

    async def test_operator_sees_group_create_form(self, app):
        """Operators with groups:write should see the Add Group form."""
        client = await _create_user(app, username="op_ui2", role_name="Operator")
        try:
            resp = await client.get("/devices")
            assert "Add Group" in resp.text
        finally:
            await client.aclose()

    async def test_viewer_no_group_create_form(self, app):
        """Viewers without groups:write should NOT see the Add Group form."""
        client = await _create_user(app, username="vw_ui2", role_name="Viewer")
        try:
            resp = await client.get("/devices")
            assert "Add Group" not in resp.text
        finally:
            await client.aclose()


@pytest.mark.asyncio
class TestSplashDisplay:
    """Verify splash screen read-only display for viewers."""

    async def test_viewer_sees_no_default_asset_text(self, app):
        """When a group has no default asset, viewer sees 'No default asset'."""
        group_id = await _create_group(app, name="Splash Test Group")
        client = await _create_user(app, username="vw_splash1", role_name="Viewer", group_ids=[group_id])
        try:
            resp = await client.get("/devices")
            assert "No default asset" in resp.text
        finally:
            await client.aclose()

    async def test_viewer_cannot_change_splash(self, app):
        """Viewer should not see the splash dropdown (setGroupDefaultAsset)."""
        group_id = await _create_group(app, name="Splash Lock Group")
        client = await _create_user(app, username="vw_splash2", role_name="Viewer", group_ids=[group_id])
        try:
            resp = await client.get("/devices")
            assert "setGroupDefaultAsset" not in resp.text
        finally:
            await client.aclose()

    async def test_operator_sees_splash_dropdown(self, app):
        """Operator with groups:write should see the splash dropdown."""
        group_id = await _create_group(app, name="Splash Op Group")
        client = await _create_user(app, username="op_splash1", role_name="Operator", group_ids=[group_id])
        try:
            resp = await client.get("/devices")
            assert "setGroupDefaultAsset" in resp.text
        finally:
            await client.aclose()

    async def test_viewer_no_delete_group_button(self, app):
        """Viewer should not see the delete group button."""
        group_id = await _create_group(app, name="No Delete Group")
        client = await _create_user(app, username="vw_splash3", role_name="Viewer", group_ids=[group_id])
        try:
            resp = await client.get("/devices")
            assert 'deleteGroup(' not in resp.text
        finally:
            await client.aclose()
