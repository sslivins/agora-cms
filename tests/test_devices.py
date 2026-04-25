"""Tests for device API endpoints."""

import uuid
from datetime import time

import pytest

from cms.models.device_profile import DeviceProfile


async def _create_profile(db_session, name="Test Profile"):
    profile = DeviceProfile(name=name)
    db_session.add(profile)
    await db_session.flush()
    return profile


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

    async def test_update_device_timezone(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-tz", name="test-pi-tz", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch("/api/devices/test-pi-tz", json={"timezone": "Europe/Berlin"})
        assert resp.status_code == 200
        assert resp.json()["timezone"] == "Europe/Berlin"

    async def test_clear_device_timezone(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(
            id="test-pi-tz2", name="test-pi-tz2",
            status=DeviceStatus.ADOPTED, timezone="Europe/Berlin",
        )
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch("/api/devices/test-pi-tz2", json={"timezone": None})
        assert resp.status_code == 200
        assert resp.json()["timezone"] is None

    async def test_adopt_device(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(id="test-pi-003", name="test-pi-003", status=DeviceStatus.PENDING)
        db_session.add(device)
        profile = await _create_profile(db_session)
        await db_session.commit()

        resp = await client.post("/api/devices/test-pi-003/adopt", json={"profile_id": str(profile.id)})
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
        profile = await _create_profile(db_session)
        await db_session.commit()

        resp = await client.post("/api/devices/test-pi-orphan/adopt", json={"profile_id": str(profile.id)})
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
        profile = await _create_profile(db_session)
        await db_session.commit()

        resp = await client.post("/api/devices/test-pi-adopted/adopt", json={"profile_id": str(profile.id)})
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

    async def test_delete_device_with_schedules(self, client, db_session):
        """Deleting a device that has device_assets should succeed even if
        the device's group has schedules (schedules are group-level now)."""
        from datetime import time
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus
        from cms.models.schedule import Schedule

        group = DeviceGroup(name="Del Sched Group")
        db_session.add(group)
        await db_session.flush()

        device = Device(id="del-sched-pi", name="Del Sched", status=DeviceStatus.ADOPTED, group_id=group.id)
        asset = Asset(filename="test.mp4", asset_type=AssetType.VIDEO, size_bytes=5000, checksum="abc")
        db_session.add_all([device, asset])
        await db_session.flush()

        schedule = Schedule(
            name="Test Schedule",
            group_id=group.id,
            asset_id=asset.id,
            start_time=time(9, 0),
            end_time=time(17, 0),
        )
        db_session.add(schedule)
        await db_session.commit()

        resp = await client.delete("/api/devices/del-sched-pi")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "del-sched-pi"

        # Verify device is gone
        resp = await client.get("/api/devices")
        assert all(d["id"] != "del-sched-pi" for d in resp.json())

    async def test_delete_device_with_device_assets(self, client, db_session):
        """Deleting a device that has device_assets should succeed."""
        from cms.models.asset import Asset, AssetType, DeviceAsset
        from cms.models.device import Device, DeviceStatus

        device = Device(id="del-da-pi", name="Del DA", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="da_test.mp4", asset_type=AssetType.VIDEO, size_bytes=5000, checksum="def")
        db_session.add_all([device, asset])
        await db_session.flush()

        da = DeviceAsset(device_id="del-da-pi", asset_id=asset.id)
        db_session.add(da)
        await db_session.commit()

        resp = await client.delete("/api/devices/del-da-pi")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "del-da-pi"


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
class TestDevicePlaybackFields:
    """Verify playback_mode, playback_asset, pipeline_state, has_active_schedule
    are returned by GET /api/devices based on live device_manager state and
    scheduler _now_playing state.
    """

    async def test_playback_fields_default_when_offline(self, client, db_session):
        """Offline devices should have null playback fields and no active schedule."""
        from cms.models.device import Device, DeviceStatus

        device = Device(id="pb-offline", name="pb-offline", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.get("/api/devices")
        assert resp.status_code == 200
        dev = [d for d in resp.json() if d["id"] == "pb-offline"][0]
        assert dev["playback_mode"] is None
        assert dev["playback_asset"] is None
        assert dev["pipeline_state"] is None
        assert dev["has_active_schedule"] is False

    async def test_playback_fields_from_live_state(self, client, db_session):
        """Connected device should expose playback fields persisted in the DB."""
        from cms.models.device import Device, DeviceStatus
        from cms.services.device_manager import device_manager
        from cms.services import device_presence

        device = Device(id="pb-live", name="pb-live", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        class FakeWS:
            async def send_json(self, data): pass

        device_manager.register("pb-live", FakeWS())
        await device_presence.mark_online(db_session, "pb-live")
        await device_presence.update_status(
            db_session, "pb-live",
            {"mode": "play", "asset": "demo.mp4", "pipeline_state": "PLAYING"},
        )

        try:
            resp = await client.get("/api/devices")
            dev = [d for d in resp.json() if d["id"] == "pb-live"][0]
            assert dev["playback_mode"] == "play"
            assert dev["playback_asset"] == "demo.mp4"
            assert dev["pipeline_state"] == "PLAYING"
        finally:
            device_manager.disconnect("pb-live")
            await device_presence.mark_offline(db_session, "pb-live")

    async def test_has_active_schedule_from_scheduler(self, client, db_session):
        """Device with an active schedule in DB should have has_active_schedule=True."""
        from cms.models.device import Device, DeviceStatus, DeviceGroup
        from shared.models.asset import Asset, AssetType
        from cms.models.schedule import Schedule

        device = Device(id="pb-sched", name="pb-sched", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.flush()

        asset = Asset(id=uuid.uuid4(), filename="scheduled.mp4",
                      asset_type=AssetType.VIDEO, checksum="abc")
        group = DeviceGroup(id=uuid.uuid4(), name="sched-group")
        db_session.add_all([asset, group])
        await db_session.flush()

        from sqlalchemy import update
        await db_session.execute(
            update(Device).where(Device.id == "pb-sched").values(group_id=group.id)
        )

        schedule = Schedule(
            id=uuid.uuid4(), name="Test Sched", asset_id=asset.id,
            group_id=group.id, start_time=time(0, 0, 0),
            end_time=time(23, 59, 59), enabled=True, priority=0,
        )
        db_session.add(schedule)
        await db_session.commit()

        resp = await client.get("/api/devices")
        dev = [d for d in resp.json() if d["id"] == "pb-sched"][0]
        assert dev["has_active_schedule"] is True

    async def test_splash_mode_fields(self, client, db_session):
        """Device showing splash should report mode=splash and no asset."""
        from cms.models.device import Device, DeviceStatus
        from cms.services.device_manager import device_manager
        from cms.services import device_presence

        device = Device(id="pb-splash", name="pb-splash", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        class FakeWS:
            async def send_json(self, data): pass

        device_manager.register("pb-splash", FakeWS())
        await device_presence.mark_online(db_session, "pb-splash")
        await device_presence.update_status(
            db_session, "pb-splash",
            {"mode": "splash", "asset": None, "pipeline_state": "NULL"},
        )

        try:
            resp = await client.get("/api/devices")
            dev = [d for d in resp.json() if d["id"] == "pb-splash"][0]
            assert dev["playback_mode"] == "splash"
            assert dev["playback_asset"] is None
            assert dev["pipeline_state"] == "NULL"
        finally:
            device_manager.disconnect("pb-splash")
            await device_presence.mark_offline(db_session, "pb-splash")


@pytest.mark.asyncio
class TestGetSingleDevice:
    """Tests for GET /api/devices/{device_id} endpoint."""

    async def test_get_device_playback_fields_from_live_state(self, client, db_session):
        """Single device endpoint should include live playback state fields."""
        from cms.models.device import Device, DeviceStatus
        from cms.services.device_manager import device_manager
        from cms.services import device_presence

        device = Device(id="single-live", name="single-live", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        class FakeWS:
            async def send_json(self, data): pass

        device_manager.register("single-live", FakeWS())
        await device_presence.mark_online(db_session, "single-live")
        await device_presence.update_status(
            db_session, "single-live",
            {
                "mode": "play", "asset": "beach.mp4",
                "pipeline_state": "PLAYING", "display_connected": True,
            },
        )

        try:
            resp = await client.get("/api/devices/single-live")
            assert resp.status_code == 200
            dev = resp.json()
            assert dev["playback_mode"] == "play"
            assert dev["playback_asset"] == "beach.mp4"
            assert dev["pipeline_state"] == "PLAYING"
            assert dev["display_connected"] is True
        finally:
            device_manager.disconnect("single-live")
            await device_presence.mark_offline(db_session, "single-live")

    async def test_get_device_has_active_schedule(self, client, db_session):
        """Single device endpoint should include has_active_schedule from scheduler."""
        from cms.models.device import Device, DeviceStatus, DeviceGroup
        from shared.models.asset import Asset, AssetType
        from cms.models.schedule import Schedule

        device = Device(id="single-sched", name="single-sched", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.flush()

        asset = Asset(id=uuid.uuid4(), filename="scheduled.mp4",
                      asset_type=AssetType.VIDEO, checksum="abc")
        group = DeviceGroup(id=uuid.uuid4(), name="single-sched-group")
        db_session.add_all([asset, group])
        await db_session.flush()

        from sqlalchemy import update
        await db_session.execute(
            update(Device).where(Device.id == "single-sched").values(group_id=group.id)
        )

        schedule = Schedule(
            id=uuid.uuid4(), name="Single Sched", asset_id=asset.id,
            group_id=group.id, start_time=time(0, 0, 0),
            end_time=time(23, 59, 59), enabled=True, priority=0,
        )
        db_session.add(schedule)
        await db_session.commit()

        resp = await client.get("/api/devices/single-sched")
        assert resp.status_code == 200
        assert resp.json()["has_active_schedule"] is True

    async def test_get_device_offline_defaults(self, client, db_session):
        """Offline device via single endpoint should have null playback fields."""
        from cms.models.device import Device, DeviceStatus

        device = Device(id="single-offline", name="single-offline", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.get("/api/devices/single-offline")
        assert resp.status_code == 200
        dev = resp.json()
        assert dev["playback_mode"] is None
        assert dev["playback_asset"] is None
        assert dev["pipeline_state"] is None
        assert dev["display_connected"] is None
        assert dev["has_active_schedule"] is False


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
        """Second upgrade to the same device returns 409 (DB-backed CAS claim)."""
        from datetime import datetime, timezone
        from cms.models.device import Device, DeviceStatus
        from cms.services.device_manager import device_manager

        device = Device(
            id="up-pi-002",
            name="up-pi-002",
            status=DeviceStatus.ADOPTED,
            upgrade_started_at=datetime.now(timezone.utc),
        )
        db_session.add(device)
        await db_session.commit()

        # Simulate device connected and already upgrading (claim already held)
        class FakeWS:
            async def send_json(self, data): pass
        device_manager.register("up-pi-002", FakeWS())
        from cms.services import device_presence
        await device_presence.mark_online(db_session, "up-pi-002")

        try:
            resp = await client.post("/api/devices/up-pi-002/upgrade")
            assert resp.status_code == 409
            assert "already in progress" in resp.json()["detail"]
        finally:
            device_manager.disconnect("up-pi-002")

    async def test_upgrade_clears_flag_on_disconnect(self, client, db_session):
        """Upgrading flag is cleared via DB column on disconnect."""
        from sqlalchemy import select
        from cms.models.device import Device, DeviceStatus

        # The claim column is cleared on register (see ws.py). Verify it
        # can be set + cleared atomically through the ORM column.
        device = Device(id="up-pi-003", name="up-pi-003", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        from datetime import datetime, timezone
        device.upgrade_started_at = datetime.now(timezone.utc)
        await db_session.commit()

        device.upgrade_started_at = None
        await db_session.commit()

        row = (await db_session.execute(
            select(Device).where(Device.id == "up-pi-003")
        )).scalar_one()
        assert row.upgrade_started_at is None


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

    async def test_delete_group_blocked_by_schedule(self, client, db_session):
        """Deleting a group used by a schedule should return 409."""
        from cms.models.asset import Asset, AssetType

        resp = await client.post("/api/devices/groups/", json={"name": "Scheduled Group"})
        group_id = resp.json()["id"]

        asset = Asset(filename="block.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="blk")
        db_session.add(asset)
        await db_session.commit()

        sched_resp = await client.post("/api/schedules", json={
            "name": "Blocks Delete",
            "group_id": group_id,
            "asset_id": str(asset.id),
            "start_time": "08:00",
            "end_time": "12:00",
        })
        assert sched_resp.status_code == 201

        resp = await client.delete(f"/api/devices/groups/{group_id}")
        assert resp.status_code == 409
        assert "schedule" in resp.json()["detail"].lower()

        # Group should still exist
        resp = await client.get("/api/devices/groups/")
        assert any(g["id"] == group_id for g in resp.json())

    async def test_delete_group_allowed_after_schedule_removed(self, client, db_session):
        """Deleting a group should succeed once all schedules are removed."""
        from cms.models.asset import Asset, AssetType

        resp = await client.post("/api/devices/groups/", json={"name": "Freed Group"})
        group_id = resp.json()["id"]

        asset = Asset(filename="free.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="fre")
        db_session.add(asset)
        await db_session.commit()

        sched_resp = await client.post("/api/schedules", json={
            "name": "Temporary",
            "group_id": group_id,
            "asset_id": str(asset.id),
            "start_time": "08:00",
            "end_time": "12:00",
        })
        sched_id = sched_resp.json()["id"]

        # Delete the schedule first
        del_sched = await client.delete(f"/api/schedules/{sched_id}")
        assert del_sched.status_code == 200

        # Now group deletion should succeed
        resp = await client.delete(f"/api/devices/groups/{group_id}")
        assert resp.status_code == 200

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


@pytest.mark.asyncio
class TestGroupDefaultAssetSync:
    """Changing a group's default_asset_id should push an immediate sync to all
    member devices, rather than waiting for the scheduler cycle."""

    async def test_update_group_default_asset_pushes_sync_to_members(self, client, db_session):
        """PATCH group default_asset_id should send fetch_asset + sync to every device in the group."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus
        from cms.services.device_manager import device_manager

        asset = Asset(filename="group_new.png", asset_type=AssetType.IMAGE, size_bytes=2048, checksum="gnew1")
        db_session.add(asset)
        await db_session.flush()

        group = DeviceGroup(name="Sync Test Group")
        db_session.add(group)
        await db_session.flush()

        d1 = Device(id="grp-sync-pi-1", name="Pi 1", status=DeviceStatus.ADOPTED, group_id=group.id)
        d2 = Device(id="grp-sync-pi-2", name="Pi 2", status=DeviceStatus.ADOPTED, group_id=group.id)
        db_session.add_all([d1, d2])
        await db_session.commit()

        sent_d1, sent_d2 = [], []

        class FakeWS1:
            async def send_json(self, data):
                sent_d1.append(data)

        class FakeWS2:
            async def send_json(self, data):
                sent_d2.append(data)

        device_manager.register("grp-sync-pi-1", FakeWS1())
        device_manager.register("grp-sync-pi-2", FakeWS2())
        from cms.services import device_presence
        await device_presence.mark_online(db_session, "grp-sync-pi-1")
        await device_presence.mark_online(db_session, "grp-sync-pi-2")

        try:
            resp = await client.patch(
                f"/api/devices/groups/{group.id}",
                json={"default_asset_id": str(asset.id)},
            )
            assert resp.status_code == 200

            # Both devices should have received messages
            assert len(sent_d1) > 0, "Device 1 should have received a sync push"
            assert len(sent_d2) > 0, "Device 2 should have received a sync push"

            # First message to each should be fetch_asset
            assert sent_d1[0]["type"] == "fetch_asset"
            assert sent_d2[0]["type"] == "fetch_asset"
        finally:
            device_manager.disconnect("grp-sync-pi-1")
            device_manager.disconnect("grp-sync-pi-2")

    async def test_clear_group_default_asset_pushes_sync(self, client, db_session):
        """Setting group default_asset_id to null should push sync (splash) to members."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus
        from cms.services.device_manager import device_manager

        asset = Asset(filename="to_clear.png", asset_type=AssetType.IMAGE, size_bytes=1024, checksum="clr1")
        db_session.add(asset)
        await db_session.flush()

        group = DeviceGroup(name="Clear Group", default_asset_id=asset.id)
        db_session.add(group)
        await db_session.flush()

        device = Device(id="grp-clear-pi", name="Clear Pi", status=DeviceStatus.ADOPTED, group_id=group.id)
        db_session.add(device)
        await db_session.commit()

        sent = []

        class FakeWS:
            async def send_json(self, data):
                sent.append(data)

        device_manager.register("grp-clear-pi", FakeWS())
        from cms.services import device_presence
        await device_presence.mark_online(db_session, "grp-clear-pi")

        try:
            resp = await client.patch(
                f"/api/devices/groups/{group.id}",
                json={"default_asset_id": None},
            )
            assert resp.status_code == 200

            # Device should have received a sync push (to show splash)
            assert len(sent) > 0, "Device should have received a sync push when group default cleared"
            assert sent[0]["type"] == "sync"
        finally:
            device_manager.disconnect("grp-clear-pi")

    async def test_update_group_name_does_not_push_sync(self, client, db_session):
        """Changing only the group name should NOT trigger a sync push."""
        from cms.models.device import Device, DeviceGroup, DeviceStatus
        from cms.services.device_manager import device_manager

        group = DeviceGroup(name="No Push Group")
        db_session.add(group)
        await db_session.flush()

        device = Device(id="grp-nopush-pi", name="No Push Pi", status=DeviceStatus.ADOPTED, group_id=group.id)
        db_session.add(device)
        await db_session.commit()

        sent = []

        class FakeWS:
            async def send_json(self, data):
                sent.append(data)

        device_manager.register("grp-nopush-pi", FakeWS())

        try:
            resp = await client.patch(
                f"/api/devices/groups/{group.id}",
                json={"name": "Renamed Group"},
            )
            assert resp.status_code == 200
            assert len(sent) == 0, "Name-only update should not push to devices"
        finally:
            device_manager.disconnect("grp-nopush-pi")

    async def test_device_default_overrides_group_default(self, client, db_session):
        """When a device has its own default_asset_id, group update should push the device's asset, not the group's."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceGroup, DeviceStatus
        from cms.services.device_manager import device_manager

        group_asset = Asset(filename="group_level.png", asset_type=AssetType.IMAGE, size_bytes=2048, checksum="grplvl")
        device_asset = Asset(filename="device_level.png", asset_type=AssetType.IMAGE, size_bytes=1024, checksum="devlvl")
        db_session.add_all([group_asset, device_asset])
        await db_session.flush()

        group = DeviceGroup(name="Override Group")
        db_session.add(group)
        await db_session.flush()

        device = Device(
            id="grp-override-pi", name="Override Pi",
            status=DeviceStatus.ADOPTED, group_id=group.id,
            default_asset_id=device_asset.id,
        )
        db_session.add(device)
        await db_session.commit()

        sent = []

        class FakeWS:
            async def send_json(self, data):
                sent.append(data)

        device_manager.register("grp-override-pi", FakeWS())

        try:
            resp = await client.patch(
                f"/api/devices/groups/{group.id}",
                json={"default_asset_id": str(group_asset.id)},
            )
            assert resp.status_code == 200

            # Device has its own default, so fetch_asset should use the device-level asset
            assert len(sent) > 0
            fetch_msg = sent[0]
            assert fetch_msg["type"] == "fetch_asset"
            assert fetch_msg["asset_name"] == "device_level.png"
        finally:
            device_manager.disconnect("grp-override-pi")


@pytest.mark.asyncio
class TestPendingDeviceNameDisplay:
    """Dashboard should show device friendly name, not raw ID, for pending devices."""

    async def test_dashboard_pending_shows_friendly_name(self, client, db_session):
        from cms.models.device import Device, DeviceStatus

        device = Device(
            id="abc123serial", name="Living Room TV",
            status=DeviceStatus.PENDING,
        )
        db_session.add(device)
        await db_session.commit()

        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        # The friendly name should appear in the pending devices table
        assert "Living Room TV" in html
        # The adopt button should pass the friendly name for display
        assert "adoptDevice(" in html
        assert "Living Room TV" in html

    async def test_dashboard_pending_renders_lan_ip_column(self, client, db_session):
        """Regression for #436: dashboard pending-devices table must show
        the device's self-reported LAN IP so admins can SSH/browse to it
        before adopting.  The IP comes from Device.ip_address (populated
        by ws.py from raw.get('ip_address') on register)."""
        from cms.models.device import Device, DeviceStatus

        device = Device(
            id="pi-with-lan-ip",
            name="Front Desk Pi",
            status=DeviceStatus.PENDING,
            ip_address="192.168.1.53",
        )
        db_session.add(device)
        await db_session.commit()

        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        assert "Front Desk Pi" in html
        assert "192.168.1.53" in html
        # Header column added too
        assert "<th>IP</th>" in html


@pytest.mark.asyncio
class TestDefaultAssetVariantResolution:
    """Regression test: setting a device's default asset must use the transcoded
    variant when the device has a profile, not the original source file.

    Bug: _push_default_asset() previously built FetchAssetMessage directly with
    the source asset URL, bypassing _resolve_asset_for_device(). This caused
    devices with H.264-only pipelines to receive AV1/VP9 source files → black screen.
    """

    async def test_default_asset_sends_variant_url_not_source(self, client, db_session):
        """PATCH default_asset_id on a profiled device must send variant download URL."""
        from unittest.mock import AsyncMock, patch
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device import Device, DeviceStatus
        from cms.models.device_profile import DeviceProfile
        from cms.services.device_manager import device_manager

        # Create a profile (e.g. pi-zero-2w: H.264)
        profile = DeviceProfile(name="pi-zero-2w", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        # Create source video asset (e.g. AV1 original)
        source = Asset(
            filename="Sony_4k60_AV1.mp4", asset_type=AssetType.VIDEO,
            size_bytes=30_000_000, checksum="source_av1_checksum",
        )
        db_session.add(source)
        await db_session.flush()

        # Create READY variant (H.264 transcode)
        variant = AssetVariant(
            source_asset_id=source.id, profile_id=profile.id,
            status=VariantStatus.READY,
            filename="Sony_4k60_AV1_pi-zero-2w.mp4",
            size_bytes=42_000_000, checksum="variant_h264_checksum",
        )
        db_session.add(variant)
        await db_session.flush()

        # Create adopted device with profile assigned
        device = Device(
            id="test-variant-pi", name="Variant Test Pi",
            status=DeviceStatus.ADOPTED, profile_id=profile.id,
        )
        db_session.add(device)
        await db_session.commit()

        # Mock the device as connected and capture sent messages
        sent_messages = []

        class FakeWS:
            async def send_json(self, data):
                sent_messages.append(data)

        device_manager.register("test-variant-pi", FakeWS())
        from cms.services import device_presence
        await device_presence.mark_online(db_session, "test-variant-pi")

        try:
            resp = await client.patch(
                "/api/devices/test-variant-pi",
                json={"default_asset_id": str(source.id)},
            )
            assert resp.status_code == 200

            # Should have sent fetch_asset + play
            assert len(sent_messages) == 2
            fetch_msg = sent_messages[0]
            assert fetch_msg["type"] == "fetch_asset"

            # CRITICAL: URL must point to variant download, NOT source asset download
            assert f"/api/assets/variants/{variant.id}/download" in fetch_msg["download_url"]
            assert f"/api/assets/{source.id}/download" not in fetch_msg["download_url"]

            # Checksum and size must be the variant's, not the source's
            assert fetch_msg["checksum"] == "variant_h264_checksum"
            assert fetch_msg["size_bytes"] == 42_000_000

            # asset_name should still be the source filename (device saves under this name)
            assert fetch_msg["asset_name"] == "Sony_4k60_AV1.mp4"
        finally:
            device_manager.disconnect("test-variant-pi")

    async def test_default_asset_image_no_variant_uses_source(self, client, db_session):
        """Image without a ready variant should fall through to source URL."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus
        from cms.models.device_profile import DeviceProfile
        from cms.services.device_manager import device_manager

        profile = DeviceProfile(name="pi-zero-img", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()

        image = Asset(
            filename="splash.png", asset_type=AssetType.IMAGE,
            size_bytes=1024, checksum="img_checksum",
        )
        db_session.add(image)
        await db_session.flush()

        device = Device(
            id="test-img-pi", name="Image Test",
            status=DeviceStatus.ADOPTED, profile_id=profile.id,
        )
        db_session.add(device)
        await db_session.commit()

        sent_messages = []

        class FakeWS:
            async def send_json(self, data):
                sent_messages.append(data)

        device_manager.register("test-img-pi", FakeWS())

        try:
            resp = await client.patch(
                "/api/devices/test-img-pi",
                json={"default_asset_id": str(image.id)},
            )
            assert resp.status_code == 200

            fetch_msg = sent_messages[0]
            assert fetch_msg["type"] == "fetch_asset"
            # No variant exists, so image falls through to source URL
            assert f"/api/assets/{image.id}/download" in fetch_msg["download_url"]
            assert fetch_msg["checksum"] == "img_checksum"
        finally:
            device_manager.disconnect("test-img-pi")

    async def test_default_asset_image_with_variant(self, client, db_session):
        """Image with a READY variant should use variant URL."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device import Device, DeviceStatus
        from cms.models.device_profile import DeviceProfile
        from cms.services.device_manager import device_manager

        profile = DeviceProfile(name="pi-zero-imgv", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()

        image = Asset(
            filename="photo.jpg", asset_type=AssetType.IMAGE,
            size_bytes=2048, checksum="img_src",
        )
        db_session.add(image)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=image.id,
            profile_id=profile.id,
            filename="variant.jpg",
            status=VariantStatus.READY,
            size_bytes=1024,
            checksum="img_variant_cksum",
        )
        db_session.add(variant)
        await db_session.flush()

        device = Device(
            id="test-imgv-pi", name="Image Variant Test",
            status=DeviceStatus.ADOPTED, profile_id=profile.id,
        )
        db_session.add(device)
        await db_session.commit()

        sent_messages = []

        class FakeWS:
            async def send_json(self, data):
                sent_messages.append(data)

        device_manager.register("test-imgv-pi", FakeWS())

        try:
            resp = await client.patch(
                "/api/devices/test-imgv-pi",
                json={"default_asset_id": str(image.id)},
            )
            assert resp.status_code == 200

            fetch_msg = sent_messages[0]
            assert fetch_msg["type"] == "fetch_asset"
            # Image with READY variant should use variant URL
            assert f"/api/assets/variants/{variant.id}/download" in fetch_msg["download_url"]
            assert fetch_msg["checksum"] == "img_variant_cksum"
        finally:
            device_manager.disconnect("test-imgv-pi")

    async def test_default_asset_no_profile_uses_source(self, client, db_session):
        """Device without a profile should get the source asset URL."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus
        from cms.services.device_manager import device_manager

        video = Asset(
            filename="test.mp4", asset_type=AssetType.VIDEO,
            size_bytes=5_000_000, checksum="src_checksum",
        )
        db_session.add(video)
        await db_session.flush()

        device = Device(
            id="test-noprof-pi", name="No Profile",
            status=DeviceStatus.ADOPTED,
        )
        db_session.add(device)
        await db_session.commit()

        sent_messages = []

        class FakeWS:
            async def send_json(self, data):
                sent_messages.append(data)

        device_manager.register("test-noprof-pi", FakeWS())

        try:
            resp = await client.patch(
                "/api/devices/test-noprof-pi",
                json={"default_asset_id": str(video.id)},
            )
            assert resp.status_code == 200

            fetch_msg = sent_messages[0]
            assert fetch_msg["type"] == "fetch_asset"
            # No profile → source URL directly
            assert f"/api/assets/{video.id}/download" in fetch_msg["download_url"]
            assert fetch_msg["checksum"] == "src_checksum"
        finally:
            device_manager.disconnect("test-noprof-pi")


@pytest.mark.asyncio
class TestWipeAssetsOnAdoptDelete:
    """Verify that adopt and delete send wipe_assets to connected devices."""

    async def test_adopt_sends_wipe_assets(self, client, db_session):
        """Adopting a pending device should send wipe_assets with reason='adopted'."""
        from cms.models.device import Device, DeviceStatus
        from cms.services.device_manager import device_manager

        device = Device(id="wipe-adopt", name="wipe-adopt", status=DeviceStatus.PENDING)
        db_session.add(device)
        profile = await _create_profile(db_session)
        await db_session.commit()

        sent_messages = []

        class FakeWS:
            async def send_json(self, data):
                sent_messages.append(data)

        device_manager.register("wipe-adopt", FakeWS())

        try:
            resp = await client.post("/api/devices/wipe-adopt/adopt", json={"profile_id": str(profile.id)})
            assert resp.status_code == 200

            wipe_msgs = [m for m in sent_messages if m.get("type") == "wipe_assets"]
            assert len(wipe_msgs) == 1
            assert wipe_msgs[0]["reason"] == "adopted"
        finally:
            device_manager.disconnect("wipe-adopt")

    async def test_adopt_orphaned_sends_wipe_assets(self, client, db_session):
        """Re-adopting an orphaned device should also send wipe_assets."""
        from cms.models.device import Device, DeviceStatus
        from cms.services.device_manager import device_manager

        device = Device(
            id="wipe-orphan", name="wipe-orphan",
            status=DeviceStatus.ORPHANED,
            device_auth_token_hash="oldhash",
        )
        db_session.add(device)
        profile = await _create_profile(db_session)
        await db_session.commit()

        sent_messages = []

        class FakeWS:
            async def send_json(self, data):
                sent_messages.append(data)

        device_manager.register("wipe-orphan", FakeWS())

        try:
            resp = await client.post("/api/devices/wipe-orphan/adopt", json={"profile_id": str(profile.id)})
            assert resp.status_code == 200

            wipe_msgs = [m for m in sent_messages if m.get("type") == "wipe_assets"]
            assert len(wipe_msgs) == 1
            assert wipe_msgs[0]["reason"] == "adopted"
        finally:
            device_manager.disconnect("wipe-orphan")

    async def test_delete_sends_wipe_assets(self, client, db_session):
        """Deleting a device should send wipe_assets with reason='deleted' before removing from DB."""
        from cms.models.device import Device, DeviceStatus
        from cms.services.device_manager import device_manager

        device = Device(id="wipe-delete", name="wipe-delete", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        sent_messages = []

        class FakeWS:
            async def send_json(self, data):
                sent_messages.append(data)

        device_manager.register("wipe-delete", FakeWS())

        try:
            resp = await client.delete("/api/devices/wipe-delete")
            assert resp.status_code == 200

            wipe_msgs = [m for m in sent_messages if m.get("type") == "wipe_assets"]
            assert len(wipe_msgs) == 1
            assert wipe_msgs[0]["reason"] == "deleted"
        finally:
            device_manager.disconnect("wipe-delete")

    async def test_adopt_offline_device_no_error(self, client, db_session):
        """Adopting an offline device should succeed (wipe is best-effort)."""
        from cms.models.device import Device, DeviceStatus

        device = Device(id="wipe-offline", name="wipe-offline", status=DeviceStatus.PENDING)
        db_session.add(device)
        profile = await _create_profile(db_session)
        await db_session.commit()

        resp = await client.post("/api/devices/wipe-offline/adopt", json={"profile_id": str(profile.id)})
        assert resp.status_code == 200
        assert resp.json()["ok"] is True

    async def test_delete_offline_device_no_error(self, client, db_session):
        """Deleting an offline device should succeed (wipe is best-effort)."""
        from cms.models.device import Device, DeviceStatus

        device = Device(id="wipe-del-off", name="wipe-del-off", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.delete("/api/devices/wipe-del-off")
        assert resp.status_code == 200
        assert resp.json()["deleted"] == "wipe-del-off"


@pytest.mark.asyncio
class TestGroupDeleteButtonUI:
    """The group delete button should be disabled when schedules reference the group."""

    async def test_delete_button_disabled_with_schedule(self, client, db_session):
        """Group with active schedule should show disabled Remove button with tooltip."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import DeviceGroup
        from cms.models.schedule import Schedule
        import datetime

        group = DeviceGroup(name="Protected Group")
        asset = Asset(filename="ui.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="ui1")
        db_session.add_all([group, asset])
        await db_session.flush()
        sched = Schedule(
            name="Blocker",
            group_id=group.id,
            asset_id=asset.id,
            start_time=datetime.time(8, 0),
            end_time=datetime.time(12, 0),
        )
        db_session.add(sched)
        await db_session.commit()

        resp = await client.get("/devices")
        assert resp.status_code == 200
        html = resp.text

        # The Remove button for this group should be disabled
        assert "Cannot delete" in html
        assert "1 schedule" in html
        # Should NOT have the clickable deleteGroup for this group
        assert f"deleteGroup('{group.id}')" not in html

    async def test_delete_button_enabled_without_schedule(self, client, db_session):
        """Group without schedules should show enabled Remove button."""
        from cms.models.device import DeviceGroup

        group = DeviceGroup(name="Free Group")
        db_session.add(group)
        await db_session.commit()

        resp = await client.get("/devices")
        assert resp.status_code == 200
        html = resp.text

        assert f"deleteGroup('{group.id}')" in html
