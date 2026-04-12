"""Tests for loop_count feature — CMS side.

Covers:
- Schedule CRUD with loop_count
- Protocol schemas (ScheduleEntry, PlayMessage) with loop_count
- Scheduler service passing loop_count to ScheduleEntry
"""

import uuid

import pytest
from pydantic import ValidationError


# ── Protocol schema tests ──


class TestProtocolLoopCount:
    def test_schedule_entry_default_none(self):
        from cms.schemas.protocol import ScheduleEntry

        entry = ScheduleEntry(
            id="s1", name="Test", asset="v.mp4",
            start_time="09:00", end_time="17:00",
        )
        assert entry.loop_count is None

    def test_schedule_entry_with_count(self):
        from cms.schemas.protocol import ScheduleEntry

        entry = ScheduleEntry(
            id="s1", name="Test", asset="v.mp4",
            start_time="09:00", end_time="17:00",
            loop_count=5,
        )
        assert entry.loop_count == 5
        data = entry.model_dump(mode="json")
        assert data["loop_count"] == 5

    def test_play_message_default_none(self):
        from cms.schemas.protocol import PlayMessage

        msg = PlayMessage(asset="v.mp4")
        assert msg.loop_count is None

    def test_play_message_with_count(self):
        from cms.schemas.protocol import PlayMessage

        msg = PlayMessage(asset="v.mp4", loop=True, loop_count=3)
        data = msg.model_dump(mode="json")
        assert data["loop_count"] == 3
        assert data["loop"] is True


# ── Schedule schema tests ──


class TestScheduleSchemaLoopCount:
    def test_create_default_none(self):
        from cms.schemas.schedule import ScheduleCreate

        sched = ScheduleCreate(
            name="Test", device_id="pi-001", asset_id=uuid.uuid4(),
            start_time="08:00", end_time="12:00",
        )
        assert sched.loop_count is None

    def test_create_with_count(self):
        from cms.schemas.schedule import ScheduleCreate

        sched = ScheduleCreate(
            name="Test", device_id="pi-001", asset_id=uuid.uuid4(),
            start_time="08:00", end_time="12:00", loop_count=10,
        )
        assert sched.loop_count == 10

    def test_update_partial_loop_count(self):
        from cms.schemas.schedule import ScheduleUpdate

        update = ScheduleUpdate(loop_count=7)
        dumped = update.model_dump(exclude_unset=True)
        assert dumped == {"loop_count": 7}

    def test_update_clear_loop_count(self):
        from cms.schemas.schedule import ScheduleUpdate

        update = ScheduleUpdate(loop_count=None)
        dumped = update.model_dump(exclude_unset=True)
        assert "loop_count" in dumped
        assert dumped["loop_count"] is None


# ── API CRUD tests ──


@pytest.mark.asyncio
class TestScheduleLoopCountCRUD:
    async def _create_device_and_asset(self, db_session):
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        device = Device(id="loop-pi", name="Loop Test", status=DeviceStatus.ADOPTED)
        asset = Asset(filename="loop.mp4", asset_type=AssetType.VIDEO, size_bytes=100, checksum="lll", duration_seconds=30.0)
        db_session.add_all([device, asset])
        await db_session.commit()
        return device.id, str(asset.id)

    async def test_create_with_loop_count(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Counted Loop",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "loop_count": 5,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["loop_count"] == 5
        # end_time auto-computed: 08:00 + 5×30s = 08:02:30
        assert data["end_time"] == "08:02:30"

    async def test_create_without_loop_count(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Infinite Loop",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        assert resp.status_code == 201
        assert resp.json()["loop_count"] is None

    async def test_update_loop_count(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Will Update",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"loop_count": 10})
        assert resp.status_code == 200
        assert resp.json()["loop_count"] == 10
        # end_time recomputed: 08:00 + 10×30s = 08:05:00
        assert resp.json()["end_time"] == "08:05:00"

    async def test_clear_loop_count(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Will Clear",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "loop_count": 5,
        })
        sched_id = create.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"loop_count": None})
        assert resp.status_code == 200
        assert resp.json()["loop_count"] is None

    async def test_loop_count_persists_through_list(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        await client.post("/api/schedules", json={
            "name": "Persistent",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
            "loop_count": 7,
        })

        resp = await client.get("/api/schedules")
        assert resp.status_code == 200
        schedules = resp.json()
        assert any(s["loop_count"] == 7 for s in schedules)
