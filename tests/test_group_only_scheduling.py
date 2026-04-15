"""Tests for group-only scheduling, device location, and adoption flow.

Covers the three features introduced in v1.25.0:
1. Schedules target groups only (no device_id)
2. Device location metadata
3. Adoption modal with name/location/group_id body
"""

import uuid
from datetime import time

import pytest

from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus


# ── Helpers ──


async def _seed_group_and_asset(db):
    """Create a group with one adopted device and a video asset."""
    group = DeviceGroup(name="Lobby Screens")
    db.add(group)
    await db.flush()

    device = Device(id="loc-pi-001", name="Lobby TV", status=DeviceStatus.ADOPTED, group_id=group.id)
    asset = Asset(filename="promo.mp4", asset_type=AssetType.VIDEO, size_bytes=5000, checksum="abc1")
    db.add_all([device, asset])
    await db.commit()
    return str(group.id), str(asset.id)


# ── 1. Group-only scheduling ──


@pytest.mark.asyncio
class TestGroupOnlyScheduling:

    async def test_create_schedule_requires_group_id(self, client, db_session):
        """Creating a schedule without group_id should fail validation."""
        _, asset_id = await _seed_group_and_asset(db_session)
        resp = await client.post("/api/schedules", json={
            "name": "No Target",
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        assert resp.status_code == 422

    async def test_create_schedule_with_group_succeeds(self, client, db_session):
        """Creating a schedule with group_id should succeed."""
        group_id, asset_id = await _seed_group_and_asset(db_session)
        resp = await client.post("/api/schedules", json={
            "name": "Morning Loop",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["group_id"] == group_id
        assert "device_id" not in data

    async def test_schedule_response_has_no_device_id(self, client, db_session):
        """Schedule API responses should never include device_id."""
        group_id, asset_id = await _seed_group_and_asset(db_session)
        resp = await client.post("/api/schedules", json={
            "name": "Check Fields",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "09:00",
            "end_time": "10:00",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert "device_id" not in data
        assert "device_name" not in data
        assert "group_id" in data

    async def test_update_schedule_group(self, client, db_session):
        """Updating a schedule's group_id should work."""
        group_id, asset_id = await _seed_group_and_asset(db_session)
        group2 = DeviceGroup(name="Cafe Screens")
        db_session.add(group2)
        await db_session.commit()

        resp = await client.post("/api/schedules", json={
            "name": "Movable",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        sched_id = resp.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={
            "group_id": str(group2.id),
        })
        assert resp.status_code == 200
        assert resp.json()["group_id"] == str(group2.id)

    async def test_list_schedules_returns_group_name(self, client, db_session):
        """Listed schedules should include group_name."""
        group_id, asset_id = await _seed_group_and_asset(db_session)
        await client.post("/api/schedules", json={
            "name": "Named Group",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        resp = await client.get("/api/schedules")
        assert resp.status_code == 200
        schedules = resp.json()
        assert len(schedules) >= 1
        s = next(s for s in schedules if s["name"] == "Named Group")
        assert s["group_name"] == "Lobby Screens"

    async def test_unknown_field_device_id_ignored(self, client, db_session):
        """Sending device_id in the body should be ignored (not cause errors)."""
        group_id, asset_id = await _seed_group_and_asset(db_session)
        resp = await client.post("/api/schedules", json={
            "name": "With Device ID",
            "group_id": group_id,
            "asset_id": asset_id,
            "device_id": "some-device",
            "start_time": "08:00",
            "end_time": "12:00",
        })
        # Pydantic ignores extra fields by default
        assert resp.status_code == 201


# ── 2. Device location ──


@pytest.mark.asyncio
class TestDeviceLocation:

    async def test_device_has_location_field(self, client, db_session):
        """Devices should expose a location field in the API."""
        device = Device(id="loc-pi-100", name="loc-pi-100", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.get("/api/devices")
        assert resp.status_code == 200
        dev = next(d for d in resp.json() if d["id"] == "loc-pi-100")
        assert "location" in dev
        assert dev["location"] == ""

    async def test_update_device_location(self, client, db_session):
        """PATCH should update the location field."""
        device = Device(id="loc-pi-101", name="loc-pi-101", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch("/api/devices/loc-pi-101", json={
            "location": "Conference Room B",
        })
        assert resp.status_code == 200
        assert resp.json()["location"] == "Conference Room B"

    async def test_clear_device_location(self, client, db_session):
        """Setting location to empty string should clear it."""
        device = Device(id="loc-pi-102", name="loc-pi-102", status=DeviceStatus.ADOPTED, location="Old Spot")
        db_session.add(device)
        await db_session.commit()

        resp = await client.patch("/api/devices/loc-pi-102", json={
            "location": "",
        })
        assert resp.status_code == 200
        assert resp.json()["location"] == ""

    async def test_location_persists_in_list(self, client, db_session):
        """Location should be visible in the device list endpoint."""
        device = Device(id="loc-pi-103", name="loc-pi-103", status=DeviceStatus.ADOPTED, location="Lobby East")
        db_session.add(device)
        await db_session.commit()

        resp = await client.get("/api/devices")
        dev = next(d for d in resp.json() if d["id"] == "loc-pi-103")
        assert dev["location"] == "Lobby East"


# ── 3. Adoption flow ──


@pytest.mark.asyncio
class TestAdoptionFlow:

    async def test_adopt_with_name(self, client, db_session):
        """Adopting with a name body should set the device name."""
        device = Device(id="adopt-name-pi", name="adopt-name-pi", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/adopt-name-pi/adopt", json={
            "name": "Reception Display",
        })
        assert resp.status_code == 200

        resp = await client.get("/api/devices")
        dev = next(d for d in resp.json() if d["id"] == "adopt-name-pi")
        assert dev["name"] == "Reception Display"
        assert dev["status"] == "adopted"

    async def test_adopt_with_location(self, client, db_session):
        """Adopting with a location body should set the device location."""
        device = Device(id="adopt-loc-pi", name="adopt-loc-pi", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/adopt-loc-pi/adopt", json={
            "location": "Main Entrance",
        })
        assert resp.status_code == 200

        resp = await client.get("/api/devices")
        dev = next(d for d in resp.json() if d["id"] == "adopt-loc-pi")
        assert dev["location"] == "Main Entrance"

    async def test_adopt_with_group(self, client, db_session):
        """Adopting with a group_id should assign the device to that group."""
        group = DeviceGroup(name="Adopt Group")
        db_session.add(group)
        await db_session.flush()

        device = Device(id="adopt-grp-pi", name="adopt-grp-pi", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/adopt-grp-pi/adopt", json={
            "group_id": str(group.id),
        })
        assert resp.status_code == 200

        resp = await client.get("/api/devices")
        dev = next(d for d in resp.json() if d["id"] == "adopt-grp-pi")
        assert dev["group_id"] == str(group.id)

    async def test_adopt_with_all_fields(self, client, db_session):
        """Adopting with name, location, and group_id all at once."""
        group = DeviceGroup(name="Full Adopt Group")
        db_session.add(group)
        await db_session.flush()

        device = Device(id="adopt-full-pi", name="adopt-full-pi", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/adopt-full-pi/adopt", json={
            "name": "Lobby Screen 1",
            "location": "Building A Lobby",
            "group_id": str(group.id),
        })
        assert resp.status_code == 200

        resp = await client.get("/api/devices")
        dev = next(d for d in resp.json() if d["id"] == "adopt-full-pi")
        assert dev["name"] == "Lobby Screen 1"
        assert dev["location"] == "Building A Lobby"
        assert dev["group_id"] == str(group.id)
        assert dev["status"] == "adopted"

    async def test_adopt_without_body_still_works(self, client, db_session):
        """Adopting without a body should still work (backwards compatible)."""
        device = Device(id="adopt-nobody-pi", name="adopt-nobody-pi", status=DeviceStatus.PENDING)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/adopt-nobody-pi/adopt")
        assert resp.status_code == 200

        resp = await client.get("/api/devices")
        dev = next(d for d in resp.json() if d["id"] == "adopt-nobody-pi")
        assert dev["status"] == "adopted"
        assert dev["name"] == "adopt-nobody-pi"  # unchanged

    async def test_adopt_already_adopted_with_body(self, client, db_session):
        """Adopting an already-adopted device should still return 400."""
        device = Device(id="adopt-dup-pi", name="adopt-dup-pi", status=DeviceStatus.ADOPTED)
        db_session.add(device)
        await db_session.commit()

        resp = await client.post("/api/devices/adopt-dup-pi/adopt", json={
            "name": "New Name",
        })
        assert resp.status_code == 400
