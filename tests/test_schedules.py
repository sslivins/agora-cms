"""Tests for schedule API endpoints."""

import pytest


@pytest.mark.asyncio
class TestScheduleCRUD:
    async def _create_device_and_asset(self, db_session):
        """Helper: create a device and an asset for scheduling."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        device = Device(id="sched-pi", name="Schedule Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="promo.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="aaa")
        db_session.add_all([device, asset])
        await db_session.commit()
        return device.id, str(asset.id)

    async def test_create_schedule(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Morning Loop",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "priority": 5,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["name"] == "Morning Loop"
        assert data["device_id"] == device_id
        assert data["asset_id"] == asset_id
        assert data["priority"] == 5
        assert data["enabled"] is True

    async def test_list_schedules(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        await client.post("/api/schedules", json={
            "name": "S1", "device_id": device_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        await client.post("/api/schedules", json={
            "name": "S2", "device_id": device_id, "asset_id": asset_id,
            "start_time": "13:00", "end_time": "17:00",
        })

        resp = await client.get("/api/schedules")
        assert resp.status_code == 200
        assert len(resp.json()) == 2

    async def test_update_schedule(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Old", "device_id": device_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"name": "Updated", "priority": 10})
        assert resp.status_code == 200
        assert resp.json()["name"] == "Updated"
        assert resp.json()["priority"] == 10

    async def test_toggle_schedule(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Toggle Me", "device_id": device_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"enabled": False})
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    async def test_delete_schedule(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Delete Me", "device_id": device_id, "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.delete(f"/api/schedules/{sched_id}")
        assert resp.status_code == 200

        resp = await client.get("/api/schedules")
        assert len(resp.json()) == 0

    async def test_schedule_requires_target(self, client, db_session):
        _, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "No Target", "asset_id": asset_id,
            "start_time": "08:00", "end_time": "12:00",
        })
        assert resp.status_code == 422  # Validation error

    async def test_schedule_with_group(self, client, db_session):
        from cms.models.asset import Asset, AssetType

        asset = Asset(filename="group-vid.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="bbb")
        db_session.add(asset)
        await db_session.commit()

        group_resp = await client.post("/api/devices/groups/", json={"name": "Lobby"})
        group_id = group_resp.json()["id"]

        resp = await client.post("/api/schedules", json={
            "name": "Group Schedule",
            "group_id": group_id,
            "asset_id": str(asset.id),
            "start_time": "09:00",
            "end_time": "18:00",
        })
        assert resp.status_code == 201
        assert resp.json()["group_id"] == group_id

    async def test_schedule_both_targets_rejected(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        group_resp = await client.post("/api/devices/groups/", json={"name": "Both"})
        group_id = group_resp.json()["id"]

        resp = await client.post("/api/schedules", json={
            "name": "Both Targets",
            "device_id": device_id,
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        assert resp.status_code == 422

    async def test_schedule_with_days_of_week(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Weekdays Only",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "17:00",
            "days_of_week": [1, 2, 3, 4, 5],
        })
        assert resp.status_code == 201
        assert resp.json()["days_of_week"] == [1, 2, 3, 4, 5]

    async def test_get_nonexistent(self, client):
        resp = await client.get("/api/schedules/00000000-0000-0000-0000-000000000000")
        assert resp.status_code == 404

    async def test_requires_auth(self, unauthed_client):
        resp = await unauthed_client.get("/api/schedules")
        assert resp.status_code in (401, 303)
