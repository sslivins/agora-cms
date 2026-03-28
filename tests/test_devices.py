"""Tests for device API endpoints."""

import pytest


@pytest.mark.asyncio
class TestListDevices:
    async def test_list_empty(self, client):
        resp = await client.get("/api/devices")
        assert resp.status_code == 200
        assert resp.json() == []

    async def test_requires_auth(self, unauthed_client):
        resp = await unauthed_client.get("/api/devices")
        assert resp.status_code in (401, 303)


@pytest.mark.asyncio
class TestDeviceCRUD:
    async def test_create_and_list_device(self, client, db_session):
        """Device created via DB (simulating WS registration), then listed via API."""
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-001", name="Living Room", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.get("/api/devices")
        assert resp.status_code == 200
        devices = resp.json()
        assert len(devices) == 1
        assert devices[0]["id"] == "test-pi-001"
        assert devices[0]["name"] == "Living Room"
        assert devices[0]["status"] == "pending"

    async def test_update_device_name(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-002", name="test-pi-002", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch("/api/devices/test-pi-002", json={"name": "Kitchen Display"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Kitchen Display"

    async def test_approve_device(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-003", name="test-pi-003", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch("/api/devices/test-pi-003", json={"status": "approved"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "approved"

    async def test_update_device_default_asset(self, client, db_session):
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        asset = Asset(filename="splash.png", asset_type=AssetType.IMAGE, size_bytes=1024, checksum="abc123")
        db_session.add(asset)
        await db_session.flush()

        device = Device(id="test-pi-004", name="test-pi-004", status=DeviceStatus.APPROVED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch(
            "/api/devices/test-pi-004",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] == str(asset.id)

    async def test_delete_device(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-005", name="test-pi-005", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.delete("/api/devices/test-pi-005")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "test-pi-005"

        resp = await client.get("/api/devices")
        assert len(resp.json()) == 0

    async def test_update_nonexistent_device(self, client):
        resp = await client.patch("/api/devices/nonexistent", json={"name": "X"})
        assert resp.status_code == 404

    async def test_delete_nonexistent_device(self, client):
        resp = await client.delete("/api/devices/nonexistent")
        assert resp.status_code == 404


@pytest.mark.asyncio
class TestDeviceGroups:
    async def test_create_group(self, client):
        resp = await client.post("/api/devices/groups/", json={"name": "Lobby Screens"})
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Lobby Screens"
        assert data["device_count"] == 0

    async def test_list_groups(self, client):
        await client.post("/api/devices/groups/", json={"name": "Group A"})
        await client.post("/api/devices/groups/", json={"name": "Group B"})

        resp = await client.get("/api/devices/groups/")
        assert resp.status_code == 200
        groups = resp.json()
        assert len(groups) == 2

    async def test_delete_group(self, client):
        resp = await client.post("/api/devices/groups/", json={"name": "Temp Group"})
        group_id = resp.json()["id"]

        resp = await client.delete(f"/api/devices/groups/{group_id}")
        assert resp.status_code == 200

        resp = await client.get("/api/devices/groups/")
        assert len(resp.json()) == 0

    async def test_assign_device_to_group(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        resp = await client.post("/api/devices/groups/", json={"name": "Test Group"})
        group_id = resp.json()["id"]

        device = Device(id="test-pi-grp", name="test-pi-grp", status=DeviceStatus.APPROVED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch("/api/devices/test-pi-grp", json={"group_id": group_id})
        assert resp.status_code == 200
        assert resp.json()["group_id"] == group_id

    async def test_group_default_asset(self, client, db_session):
        from cms.models.asset import Asset, AssetType

        asset = Asset(filename="lobby.mp4", asset_type=AssetType.VIDEO, size_bytes=5000, checksum="def456")
        db_session.add(asset)
        await db_session.commit()

        resp = await client.post(
            "/api/devices/groups/",
            json={"name": "Lobby", "default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 201
        assert resp.json()["default_asset_id"] == str(asset.id)

    async def test_update_group(self, client, db_session):
        resp = await client.post("/api/devices/groups/", json={"name": "Old Name"})
        group_id = resp.json()["id"]

        resp = await client.patch(
            f"/api/devices/groups/{group_id}",
            json={"name": "New Name", "description": "Updated"},
        )
        assert resp.status_code == 200
        assert resp.json()["name"] == "New Name"
        assert resp.json()["description"] == "Updated"
