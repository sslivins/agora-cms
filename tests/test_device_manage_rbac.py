"""Tests for devices:manage RBAC enforcement (#209).

Ensures that admin-only device actions (adopt, reboot, delete, update
firmware, change password, SSH toggle, local API toggle, factory reset,
profile/timezone changes) require the ``devices:manage`` permission and
are hidden from operators in the UI.
"""

import uuid

import pytest
import pytest_asyncio

from cms.models.device_profile import DeviceProfile
from httpx import ASGITransport, AsyncClient

from cms.permissions import (
    DEVICES_MANAGE,
    DEVICES_REBOOT,
    DEVICES_DELETE,
    OPERATOR_PERMISSIONS,
    ADMIN_PERMISSIONS,
    has_permission,
)


# ── Unit tests for permission model ──


class TestPermissionModel:
    """Verify the devices:manage permission constant and backward compat."""

    def test_manage_in_admin_permissions(self):
        assert DEVICES_MANAGE in ADMIN_PERMISSIONS

    def test_manage_not_in_operator_permissions(self):
        assert DEVICES_MANAGE not in OPERATOR_PERMISSIONS

    def test_reboot_not_in_all_permissions(self):
        """devices:reboot is legacy — should not be in ALL_PERMISSIONS."""
        from cms.permissions import ALL_PERMISSIONS
        assert DEVICES_REBOOT not in ALL_PERMISSIONS

    def test_delete_not_in_all_permissions(self):
        """devices:delete is legacy — should not be in ALL_PERMISSIONS."""
        from cms.permissions import ALL_PERMISSIONS
        assert DEVICES_DELETE not in ALL_PERMISSIONS

    def test_has_permission_direct(self):
        assert has_permission([DEVICES_MANAGE], DEVICES_MANAGE)

    def test_has_permission_backward_compat_reboot(self):
        """Legacy devices:reboot should grant devices:manage."""
        assert has_permission([DEVICES_REBOOT], DEVICES_MANAGE)

    def test_has_permission_backward_compat_delete(self):
        """Legacy devices:delete should grant devices:manage."""
        assert has_permission([DEVICES_DELETE], DEVICES_MANAGE)

    def test_has_permission_operator_no_manage(self):
        assert not has_permission(OPERATOR_PERMISSIONS, DEVICES_MANAGE)


# ── Helper to create an operator user and client ──


@pytest_asyncio.fixture
async def operator_client(app):
    """Authenticated HTTP client logged in as an operator user."""
    from sqlalchemy import select
    from cms.database import get_db
    from cms.models.user import Role, User
    from cms.auth import hash_password

    factory = app.dependency_overrides[get_db]

    # Get a DB session from the test factory
    async for db in factory():
        result = await db.execute(select(Role).where(Role.name == "Operator"))
        op_role = result.scalar_one()

        op_user = User(
            username="operator_test",
            email="operator@test.com",
            display_name="Test Operator",
            password_hash=hash_password("operatorpass"),
            role_id=op_role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(op_user)
        await db.commit()
        break

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/login", data={"username": "operator_test", "password": "operatorpass"}, follow_redirects=False)
        yield ac


@pytest_asyncio.fixture
async def test_device(app):
    """Create a test device in the DB and return its ID."""
    from sqlalchemy import select
    from cms.database import get_db
    from cms.models.device import Device, DeviceStatus

    factory = app.dependency_overrides[get_db]
    device_id = "test-device-rbac-001"

    async for db in factory():
        device = Device(
            id=device_id,
            status=DeviceStatus.ADOPTED,
            name="RBAC Test Device",
        )
        db.add(device)
        await db.commit()
        break

    return device_id


# ── API endpoint enforcement tests ──


@pytest.mark.asyncio
class TestDeviceManageEndpoints:
    """Verify that admin-only endpoints reject operators with 403."""

    async def test_admin_can_check_updates(self, client):
        resp = await client.post("/api/devices/check-updates")
        # Should succeed (200) or timeout (504) — not 403
        assert resp.status_code != 403

    async def test_operator_cannot_check_updates(self, operator_client):
        resp = await operator_client.post("/api/devices/check-updates")
        assert resp.status_code == 403

    async def test_operator_cannot_adopt(self, operator_client, test_device):
        # First make device pending so adopt is valid
        from cms.database import get_db
        from cms.models.device import Device, DeviceStatus
        from sqlalchemy import select

        factory = operator_client._transport.app.dependency_overrides[get_db]
        async for db in factory():
            result = await db.execute(select(Device).where(Device.id == test_device))
            d = result.scalar_one()
            d.status = DeviceStatus.PENDING
            await db.commit()
            break

        resp = await operator_client.post(f"/api/devices/{test_device}/adopt")
        assert resp.status_code == 403

    async def test_admin_can_adopt(self, client, test_device):
        from cms.database import get_db
        from cms.models.device import Device, DeviceStatus
        from sqlalchemy import select

        factory = client._transport.app.dependency_overrides[get_db]
        async for db in factory():
            result = await db.execute(select(Device).where(Device.id == test_device))
            d = result.scalar_one()
            d.status = DeviceStatus.PENDING
            profile = DeviceProfile(name="Test Profile")
            db.add(profile)
            await db.flush()
            profile_id = str(profile.id)
            await db.commit()
            break

        resp = await client.post(f"/api/devices/{test_device}/adopt", json={"profile_id": profile_id})
        assert resp.status_code == 200

    async def test_operator_cannot_reboot(self, operator_client, test_device):
        resp = await operator_client.post(f"/api/devices/{test_device}/reboot")
        assert resp.status_code == 403

    async def test_operator_cannot_delete(self, operator_client, test_device):
        resp = await operator_client.delete(f"/api/devices/{test_device}")
        assert resp.status_code == 403

    async def test_operator_cannot_set_password(self, operator_client, test_device):
        resp = await operator_client.post(
            f"/api/devices/{test_device}/password",
            json={"password": "newpass123"},
        )
        assert resp.status_code == 403

    async def test_operator_cannot_toggle_ssh(self, operator_client, test_device):
        resp = await operator_client.post(
            f"/api/devices/{test_device}/ssh",
            json={"enabled": False},
        )
        assert resp.status_code == 403

    async def test_operator_cannot_toggle_local_api(self, operator_client, test_device):
        resp = await operator_client.post(
            f"/api/devices/{test_device}/local-api",
            json={"enabled": False},
        )
        assert resp.status_code == 403

    async def test_operator_cannot_factory_reset(self, operator_client, test_device):
        resp = await operator_client.post(f"/api/devices/{test_device}/factory-reset")
        assert resp.status_code == 403

    async def test_operator_cannot_upgrade(self, operator_client, test_device):
        resp = await operator_client.post(f"/api/devices/{test_device}/upgrade")
        assert resp.status_code == 403

    async def test_operator_can_rename(self, operator_client, test_device):
        resp = await operator_client.patch(
            f"/api/devices/{test_device}",
            json={"name": "Renamed by Operator"},
        )
        assert resp.status_code == 200

    async def test_operator_can_set_default_asset(self, operator_client, test_device):
        resp = await operator_client.patch(
            f"/api/devices/{test_device}",
            json={"default_asset_id": None},
        )
        assert resp.status_code == 200

    async def test_operator_cannot_change_profile(self, operator_client, test_device):
        resp = await operator_client.patch(
            f"/api/devices/{test_device}",
            json={"profile_id": str(uuid.uuid4())},
        )
        assert resp.status_code == 403

    async def test_operator_cannot_change_timezone(self, operator_client, test_device):
        resp = await operator_client.patch(
            f"/api/devices/{test_device}",
            json={"timezone": "America/Chicago"},
        )
        assert resp.status_code == 403

    async def test_admin_can_change_profile(self, client, test_device):
        resp = await client.patch(
            f"/api/devices/{test_device}",
            json={"profile_id": None},
        )
        assert resp.status_code == 200

    async def test_admin_can_change_timezone(self, client, test_device):
        resp = await client.patch(
            f"/api/devices/{test_device}",
            json={"timezone": "America/Chicago"},
        )
        assert resp.status_code == 200


# ── UI visibility tests ──


@pytest.mark.asyncio
class TestDeviceManageUI:
    """Verify that admin-only controls are hidden for operators."""

    async def test_operator_no_check_updates_button(self, operator_client):
        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        # The button element should not be rendered (JS function reference is ok)
        assert 'id="check-updates-btn"' not in resp.text

    async def test_admin_sees_check_updates_button(self, client):
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert "check-updates-btn" in resp.text

    async def test_operator_no_profile_column(self, operator_client):
        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        assert ">Profile<" not in resp.text

    async def test_admin_sees_profile_column(self, client):
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert ">Profile<" in resp.text

    async def test_operator_no_adopt_button(self, operator_client, test_device):
        # Make device pending
        from cms.database import get_db
        from cms.models.device import Device, DeviceStatus
        from sqlalchemy import select

        factory = operator_client._transport.app.dependency_overrides[get_db]
        async for db in factory():
            result = await db.execute(select(Device).where(Device.id == test_device))
            d = result.scalar_one()
            d.status = DeviceStatus.PENDING
            await db.commit()
            break

        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        # Operator should not have manage permission → __canManage = false
        assert "__canManage = false" in resp.text

    async def test_operator_no_delete_button(self, operator_client, test_device):
        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        assert "__canManage = false" in resp.text

    async def test_admin_sees_delete_button(self, client, test_device):
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert "__canManage = true" in resp.text
