"""Tests for hiding pending/orphaned devices from users without devices:manage.

Users without the ``devices:manage`` permission should not see pending or
orphaned devices on the dashboard, devices page, or API list endpoint.
"""

import pytest
import pytest_asyncio

from httpx import ASGITransport, AsyncClient

from cms.models.device import Device, DeviceStatus


# ── Helpers ──


@pytest_asyncio.fixture
async def operator_client(app):
    """Authenticated HTTP client logged in as an operator (no devices:manage)."""
    from sqlalchemy import select
    from cms.database import get_db
    from cms.models.user import Role, User
    from cms.auth import hash_password

    factory = app.dependency_overrides[get_db]

    async for db in factory():
        result = await db.execute(select(Role).where(Role.name == "Operator"))
        op_role = result.scalar_one()

        op_user = User(
            username="operator_vis",
            email="operator_vis@test.com",
            display_name="Visibility Test Operator",
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
        await ac.post("/login", data={"username": "operator_vis", "password": "operatorpass"}, follow_redirects=False)
        yield ac


@pytest_asyncio.fixture
async def seed_devices(app):
    """Create one device of each status: pending, orphaned, adopted."""
    from cms.database import get_db

    factory = app.dependency_overrides[get_db]
    ids = {
        "pending": "vis-test-pending-001",
        "orphaned": "vis-test-orphaned-001",
        "adopted": "vis-test-adopted-001",
    }

    async for db in factory():
        db.add(Device(id=ids["pending"], status=DeviceStatus.PENDING, name="Pending Device"))
        db.add(Device(id=ids["orphaned"], status=DeviceStatus.ORPHANED, name="Orphaned Device"))
        db.add(Device(id=ids["adopted"], status=DeviceStatus.ADOPTED, name="Adopted Device"))
        await db.commit()
        break

    return ids


# ── API list endpoint ──


@pytest.mark.asyncio
class TestDeviceListAPI:
    """GET /api/devices should hide pending/orphaned from operators."""

    async def test_admin_sees_all_statuses(self, client, seed_devices):
        resp = await client.get("/api/devices")
        assert resp.status_code == 200
        device_ids = {d["id"] for d in resp.json()}
        assert seed_devices["pending"] in device_ids
        assert seed_devices["orphaned"] in device_ids
        assert seed_devices["adopted"] in device_ids

    async def test_operator_sees_only_adopted(self, operator_client, seed_devices):
        resp = await operator_client.get("/api/devices")
        assert resp.status_code == 200
        device_ids = {d["id"] for d in resp.json()}
        assert seed_devices["adopted"] not in device_ids  # operator has no group access to ungrouped
        assert seed_devices["pending"] not in device_ids
        assert seed_devices["orphaned"] not in device_ids


# ── Dashboard HTML ──


@pytest.mark.asyncio
class TestDashboardVisibility:
    """Dashboard should hide pending/orphaned sections from operators."""

    async def test_admin_sees_pending_on_dashboard(self, client, seed_devices):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Pending Device" in resp.text

    async def test_admin_sees_orphaned_on_dashboard(self, client, seed_devices):
        resp = await client.get("/")
        assert resp.status_code == 200
        assert "Orphaned Device" in resp.text

    async def test_operator_no_pending_on_dashboard(self, operator_client, seed_devices):
        resp = await operator_client.get("/")
        assert resp.status_code == 200
        assert "Pending Device" not in resp.text

    async def test_operator_no_orphaned_on_dashboard(self, operator_client, seed_devices):
        resp = await operator_client.get("/")
        assert resp.status_code == 200
        assert "Orphaned Device" not in resp.text


# ── Dashboard JSON polling endpoint ──


@pytest.mark.asyncio
class TestDashboardJSONVisibility:
    """GET /api/dashboard should return empty pending/orphaned for operators."""

    async def test_admin_pending_ids_populated(self, client, seed_devices):
        resp = await client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert seed_devices["pending"] in data["pending_ids"]

    async def test_admin_orphaned_ids_populated(self, client, seed_devices):
        resp = await client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert seed_devices["orphaned"] in data["orphaned_ids"]

    async def test_operator_pending_ids_empty(self, operator_client, seed_devices):
        resp = await operator_client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["pending_ids"] == []

    async def test_operator_orphaned_ids_empty(self, operator_client, seed_devices):
        resp = await operator_client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert data["orphaned_ids"] == []


# ── Devices page HTML ──


@pytest.mark.asyncio
class TestDevicesPageVisibility:
    """GET /devices should hide pending/orphaned devices from operators."""

    async def test_admin_sees_pending_on_devices_page(self, client, seed_devices):
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert "Pending Device" in resp.text

    async def test_admin_sees_orphaned_on_devices_page(self, client, seed_devices):
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert "Orphaned Device" in resp.text

    async def test_operator_no_pending_on_devices_page(self, operator_client, seed_devices):
        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        assert "Pending Device" not in resp.text

    async def test_operator_no_orphaned_on_devices_page(self, operator_client, seed_devices):
        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        assert "Orphaned Device" not in resp.text


# ── Devices page column visibility ──


@pytest.mark.asyncio
class TestDevicesPageColumns:
    """Verify Actions/Profile columns are hidden for operators, and column alignment is correct."""

    async def test_admin_sees_actions_column(self, client, seed_devices):
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert ">Actions<" in resp.text

    async def test_admin_sees_profile_column(self, client, seed_devices):
        resp = await client.get("/devices")
        assert resp.status_code == 200
        assert ">Profile<" in resp.text

    async def test_operator_no_actions_column(self, operator_client, seed_devices):
        """The main Devices table should not have an Actions header for operators."""
        import re
        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        # Find the first thead (main Devices table) and verify no Actions header
        thead_match = re.search(r"<thead>(.*?)</thead>", resp.text, re.DOTALL)
        assert thead_match
        assert ">Actions<" not in thead_match.group(1)

    async def test_operator_no_profile_column(self, operator_client, seed_devices):
        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        assert ">Profile<" not in resp.text

    async def test_operator_storage_column_removed(self, operator_client, seed_devices):
        """Storage column was removed in favor of the triage bar Storage Critical chip."""
        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        assert "<th>Storage</th>" not in resp.text

    async def test_operator_column_count_matches(self, operator_client, seed_devices):
        """Without manage, header should have 5 columns (expand, name, status, group, splash)."""
        import re
        resp = await operator_client.get("/devices")
        assert resp.status_code == 200
        # Find the first thead and count <th> tags in it
        thead_match = re.search(r"<thead>(.*?)</thead>", resp.text, re.DOTALL)
        assert thead_match
        th_count = thead_match.group(1).count("<th")
        assert th_count == 5

    async def test_admin_column_count_matches(self, client, seed_devices):
        """With manage, header should have 7 columns (expand, name, status, group, splash, profile, actions)."""
        import re
        resp = await client.get("/devices")
        assert resp.status_code == 200
        thead_match = re.search(r"<thead>(.*?)</thead>", resp.text, re.DOTALL)
        assert thead_match
        th_count = thead_match.group(1).count("<th")
        assert th_count == 7
