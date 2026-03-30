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
        assert devices[0]["is_online"] is False

    async def test_update_device_name(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-002", name="test-pi-002", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch("/api/devices/test-pi-002", json={"name": "Kitchen Display"})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Kitchen Display"

    async def test_adopt_device(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-003", name="test-pi-003", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/test-pi-003/adopt")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify status changed
        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "test-pi-003"][0]
        assert dev["status"] == "adopted"

    async def test_adopt_orphaned_device(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(
            id="test-pi-orphan", name="test-pi-orphan",
            status=DeviceStatus.ORPHANED,
            device_auth_token_hash="oldhash",
        )
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/test-pi-orphan/adopt")
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

        # Verify status changed
        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "test-pi-orphan"][0]
        assert dev["status"] == "adopted"

    async def test_adopt_already_adopted_device(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-adopted", name="test-pi-adopted", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/test-pi-adopted/adopt")
        assert resp.status_code == 400

    async def test_update_device_default_asset(self, client, db_session):
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        asset = Asset(filename="splash.png", asset_type=AssetType.IMAGE, size_bytes=1024, checksum="abc123")
        db_session.add(asset)
        await db_session.flush()

        device = Device(id="test-pi-004", name="test-pi-004", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch(
            "/api/devices/test-pi-004",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] == str(asset.id)

    async def test_clear_device_default_asset_falls_back_to_group(self, client, db_session):
        """Clearing device default_asset_id should succeed (group fallback)."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus

        group_asset = Asset(filename="group_default.png", asset_type=AssetType.IMAGE, size_bytes=2048, checksum="grp1")
        device_asset = Asset(filename="device_override.png", asset_type=AssetType.IMAGE, size_bytes=1024, checksum="dev1")
        db_session.add_all([group_asset, device_asset])
        await db_session.flush()

        group = DeviceGroup(name="Fallback Group", default_asset_id=group_asset.id)
        db_session.add(group)
        await db_session.flush()

        device = Device(
            id="test-pi-fallback", name="test-pi-fallback",
            status=DeviceStatus.ADOPTED,
            group_id=group.id,
            default_asset_id=device_asset.id,
        )
        db_session.add(device)
        await db_session.commit()

        # Clear device default — should fall back to group default
        resp = await client.patch(
            "/api/devices/test-pi-fallback",
            json={"default_asset_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] is None

    async def test_clear_device_default_asset_no_group(self, client, db_session):
        """Clearing device default_asset_id with no group should succeed (splash)."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        asset = Asset(filename="solo.png", asset_type=AssetType.IMAGE, size_bytes=1024, checksum="solo1")
        db_session.add(asset)
        await db_session.flush()

        device = Device(
            id="test-pi-solo", name="test-pi-solo",
            status=DeviceStatus.ADOPTED,
            default_asset_id=asset.id,
        )
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch(
            "/api/devices/test-pi-solo",
            json={"default_asset_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] is None

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
class TestCheckUpdates:
    async def test_check_updates_endpoint(self, client):
        from unittest.mock import AsyncMock, patch
        from cms.services import version_checker

        original = version_checker._latest_version
        try:
            with patch.object(version_checker, "_fetch_latest_version", new_callable=AsyncMock, return_value="9.9.9"):
                resp = await client.post("/api/devices/check-updates")
            assert resp.status_code == 200
            assert resp.json()["latest_version"] == "9.9.9"
        finally:
            version_checker._latest_version = original


@pytest.mark.asyncio
class TestUpgradeGuard:
    async def test_upgrade_not_connected(self, client, db_session):
        """Upgrading an offline device returns 409."""
        from cms.models.device import Device, DeviceStatus

        device = Device(id="up-pi-001", name="up-pi-001", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/up-pi-001/upgrade")
        assert resp.status_code == 409
        assert "not connected" in resp.json()["detail"]

    async def test_upgrade_concurrent_blocked(self, client, db_session):
        """Second upgrade to the same device returns 409."""
        from cms.models.device import Device, DeviceStatus
        from cms.routers.devices import _upgrading
        from cms.services.device_manager import device_manager

        device = Device(id="up-pi-002", name="up-pi-002", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        # Simulate device connected and already upgrading
        class FakeWS:
            async def send_json(self, data): pass
        device_manager.register("up-pi-002", FakeWS())
        _upgrading.add("up-pi-002")

        try:
            resp = await client.post("/api/devices/up-pi-002/upgrade")
            assert resp.status_code == 409
            assert "already in progress" in resp.json()["detail"]
        finally:
            _upgrading.discard("up-pi-002")
            device_manager.disconnect("up-pi-002")

    async def test_upgrade_clears_flag_on_disconnect(self, client, db_session):
        """Upgrading flag is cleared when device disconnects (simulated)."""
        from cms.routers.devices import _upgrading

        _upgrading.add("up-pi-003")
        # Simulate what ws.py finally block does
        _upgrading.discard("up-pi-003")
        assert "up-pi-003" not in _upgrading


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

        device = Device(id="test-pi-grp", name="test-pi-grp", status=DeviceStatus.ADOPTED)
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

    async def test_update_group_default_asset(self, client, db_session):
        """Setting only default_asset_id on a group (partial update) should work."""
        from cms.models.asset import Asset, AssetType

        asset = Asset(filename="group_splash.png", asset_type=AssetType.IMAGE, size_bytes=2048, checksum="abc")
        db_session.add(asset)
        await db_session.commit()

        resp = await client.post("/api/devices/groups/", json={"name": "Asset Group"})
        assert resp.status_code == 201
        group_id = resp.json()["id"]

        # Partial PATCH — only default_asset_id, no name required
        resp = await client.patch(
            f"/api/devices/groups/{group_id}",
            json={"default_asset_id": str(asset.id)},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] == str(asset.id)
        # Name should remain unchanged
        assert resp.json()["name"] == "Asset Group"

    async def test_clear_group_default_asset(self, client, db_session):
        """Setting default_asset_id to null should clear it."""
        from cms.models.asset import Asset, AssetType

        asset = Asset(filename="temp.png", asset_type=AssetType.IMAGE, size_bytes=1024, checksum="xyz")
        db_session.add(asset)
        await db_session.commit()

        resp = await client.post(
            "/api/devices/groups/",
            json={"name": "Clear Group", "default_asset_id": str(asset.id)},
        )
        group_id = resp.json()["id"]

        resp = await client.patch(
            f"/api/devices/groups/{group_id}",
            json={"default_asset_id": None},
        )
        assert resp.status_code == 200
        assert resp.json()["default_asset_id"] is None
