"""Tests for loop_count feature — CMS side.

Covers:
- Schedule CRUD with loop_count
- Protocol schemas (ScheduleEntry, PlayMessage) with loop_count
- Scheduler service passing loop_count to ScheduleEntry
- Auto-computation of end_time from loop_count × asset duration
- Recomputation on update when loop_count, asset_id, or start_time change
- Edge cases: no duration, midnight wrap, clearing loop_count
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

    def test_create_requires_end_time_or_loop_count(self):
        """Omitting both end_time and loop_count should fail validation."""
        from cms.schemas.schedule import ScheduleCreate

        with pytest.raises(ValidationError, match="end_time is required"):
            ScheduleCreate(
                name="Test", device_id="pi-001", asset_id=uuid.uuid4(),
                start_time="08:00",
            )

    def test_create_with_only_loop_count_no_end_time(self):
        """loop_count alone (no end_time) should pass schema validation."""
        from cms.schemas.schedule import ScheduleCreate

        sched = ScheduleCreate(
            name="Test", device_id="pi-001", asset_id=uuid.uuid4(),
            start_time="08:00", loop_count=3,
        )
        assert sched.loop_count == 3
        assert sched.end_time is None

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


# ── _compute_end_time unit tests ──


class TestComputeEndTime:
    def test_basic_computation(self):
        from datetime import time as dt_time
        from cms.routers.schedules import _compute_end_time

        # 08:00 + 5×30s = 08:02:30
        result = _compute_end_time(dt_time(8, 0), loop_count=5, duration_seconds=30.0)
        assert result == dt_time(8, 2, 30)

    def test_long_duration(self):
        from datetime import time as dt_time
        from cms.routers.schedules import _compute_end_time

        # 09:00 + 3×3600s (1h each) = 12:00:00
        result = _compute_end_time(dt_time(9, 0), loop_count=3, duration_seconds=3600.0)
        assert result == dt_time(12, 0, 0)

    def test_midnight_wraparound(self):
        from datetime import time as dt_time
        from cms.routers.schedules import _compute_end_time

        # 23:50 + 2×600s (10min each) = 00:10:00 (wraps past midnight)
        result = _compute_end_time(dt_time(23, 50), loop_count=2, duration_seconds=600.0)
        assert result == dt_time(0, 10, 0)

    def test_single_short_clip(self):
        from datetime import time as dt_time
        from cms.routers.schedules import _compute_end_time

        # 14:00 + 1×3s = 14:00:03
        result = _compute_end_time(dt_time(14, 0), loop_count=1, duration_seconds=3.0)
        assert result == dt_time(14, 0, 3)

    def test_fractional_duration(self):
        from datetime import time as dt_time
        from cms.routers.schedules import _compute_end_time

        # 10:00 + 4×7.5s = 10:00:30
        result = _compute_end_time(dt_time(10, 0), loop_count=4, duration_seconds=7.5)
        assert result == dt_time(10, 0, 30)


# ── API CRUD tests ──


@pytest.mark.asyncio
class TestScheduleLoopCountCRUD:
    async def _create_device_and_asset(self, db_session, duration=30.0):
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        device = Device(id="loop-pi", name="Loop Test", status=DeviceStatus.ADOPTED)
        asset = Asset(
            filename="loop.mp4", asset_type=AssetType.VIDEO,
            size_bytes=100, checksum="lll",
            duration_seconds=duration,
        )
        db_session.add_all([device, asset])
        await db_session.commit()
        return device.id, str(asset.id)

    async def _create_second_asset(self, db_session, duration=60.0):
        """Create a second asset with a different duration for swap tests."""
        from cms.models.asset import Asset, AssetType

        asset = Asset(
            filename="long.mp4", asset_type=AssetType.VIDEO,
            size_bytes=200, checksum="mmm",
            duration_seconds=duration,
        )
        db_session.add(asset)
        await db_session.commit()
        return str(asset.id)

    # ── Create tests ──

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

    async def test_create_end_time_overridden_by_loop_count(self, client, db_session):
        """User-provided end_time should be overridden when loop_count is set."""
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Override End",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "23:59",  # this should be ignored
            "loop_count": 2,
        })
        assert resp.status_code == 201
        data = resp.json()
        # 08:00 + 2×30s = 08:01:00, NOT 23:59
        assert data["end_time"] == "08:01:00"
        assert data["loop_count"] == 2

    async def test_create_with_loop_count_no_end_time(self, client, db_session):
        """Creating with loop_count and no end_time should work — end_time is computed."""
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "No End Time",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "10:00",
            "loop_count": 3,
        })
        assert resp.status_code == 201
        data = resp.json()
        # 10:00 + 3×30s = 10:01:30
        assert data["end_time"] == "10:01:30"
        assert data["loop_count"] == 3

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
        data = resp.json()
        assert data["loop_count"] is None
        # end_time preserved as provided
        assert data["end_time"] == "12:00:00"

    async def test_create_no_end_time_no_loop_count_fails(self, client, db_session):
        """Omitting both end_time and loop_count should fail."""
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Bad Schedule",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
        })
        assert resp.status_code == 422

    async def test_create_loop_count_no_duration_fails(self, client, db_session):
        """Asset without duration_seconds should fail when loop_count is set."""
        device_id, asset_id = await self._create_device_and_asset(db_session, duration=None)

        resp = await client.post("/api/schedules", json={
            "name": "No Duration",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "loop_count": 5,
        })
        assert resp.status_code == 422
        assert "no duration" in resp.json()["detail"].lower()

    # ── Update tests: setting loop_count ──

    async def test_update_set_loop_count(self, client, db_session):
        """Setting loop_count on a schedule without one should recompute end_time."""
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Will Update",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        assert create.status_code == 201
        sched_id = create.json()["id"]
        assert create.json()["end_time"] == "12:00:00"

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"loop_count": 10})
        assert resp.status_code == 200
        data = resp.json()
        assert data["loop_count"] == 10
        # end_time recomputed: 08:00 + 10×30s = 08:05:00
        assert data["end_time"] == "08:05:00"

    async def test_update_change_loop_count(self, client, db_session):
        """Changing loop_count value should recompute end_time."""
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Change Loops",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "09:00",
            "loop_count": 4,
        })
        sched_id = create.json()["id"]
        # 09:00 + 4×30s = 09:02:00
        assert create.json()["end_time"] == "09:02:00"

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"loop_count": 8})
        assert resp.status_code == 200
        # 09:00 + 8×30s = 09:04:00
        assert resp.json()["end_time"] == "09:04:00"
        assert resp.json()["loop_count"] == 8

    # ── Update tests: clearing loop_count ──

    async def test_clear_loop_count(self, client, db_session):
        """Clearing loop_count should preserve existing end_time."""
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Will Clear",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "loop_count": 5,
        })
        sched_id = create.json()["id"]
        # end_time was auto-computed to 08:02:30
        assert create.json()["end_time"] == "08:02:30"

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"loop_count": None})
        assert resp.status_code == 200
        data = resp.json()
        assert data["loop_count"] is None
        # end_time should stay at what it was (no recomputation)
        assert data["end_time"] == "08:02:30"

    # ── Update tests: changing asset_id with active loop_count ──

    async def test_update_asset_recomputes_end_time(self, client, db_session):
        """Changing asset_id on a schedule with loop_count should recompute end_time."""
        device_id, asset_id = await self._create_device_and_asset(db_session, duration=30.0)
        asset2_id = await self._create_second_asset(db_session, duration=60.0)

        create = await client.post("/api/schedules", json={
            "name": "Asset Swap",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "10:00",
            "loop_count": 3,
        })
        sched_id = create.json()["id"]
        # 10:00 + 3×30s = 10:01:30
        assert create.json()["end_time"] == "10:01:30"

        # Swap to 60s asset
        resp = await client.patch(f"/api/schedules/{sched_id}", json={"asset_id": asset2_id})
        assert resp.status_code == 200
        # 10:00 + 3×60s = 10:03:00
        assert resp.json()["end_time"] == "10:03:00"
        assert resp.json()["loop_count"] == 3  # unchanged

    async def test_update_asset_without_loop_count_no_recompute(self, client, db_session):
        """Changing asset_id on a schedule WITHOUT loop_count should NOT touch end_time."""
        device_id, asset_id = await self._create_device_and_asset(db_session, duration=30.0)
        asset2_id = await self._create_second_asset(db_session, duration=60.0)

        create = await client.post("/api/schedules", json={
            "name": "No Recompute",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "10:00",
            "end_time": "18:00",
        })
        sched_id = create.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"asset_id": asset2_id})
        assert resp.status_code == 200
        # end_time should stay at 18:00:00 — no loop_count, no recomputation
        assert resp.json()["end_time"] == "18:00:00"

    # ── Update tests: changing start_time with active loop_count ──

    async def test_update_start_time_recomputes_end_time(self, client, db_session):
        """Changing start_time on a schedule with loop_count should recompute end_time."""
        device_id, asset_id = await self._create_device_and_asset(db_session, duration=30.0)

        create = await client.post("/api/schedules", json={
            "name": "Start Shift",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "loop_count": 4,
        })
        sched_id = create.json()["id"]
        # 08:00 + 4×30s = 08:02:00
        assert create.json()["end_time"] == "08:02:00"

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"start_time": "14:00:00"})
        assert resp.status_code == 200
        # 14:00 + 4×30s = 14:02:00
        assert resp.json()["end_time"] == "14:02:00"
        assert resp.json()["loop_count"] == 4  # unchanged

    async def test_update_start_time_without_loop_count_no_recompute(self, client, db_session):
        """Changing start_time WITHOUT loop_count should NOT touch end_time."""
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "No Recompute Start",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={"start_time": "09:00:00"})
        assert resp.status_code == 200
        assert resp.json()["end_time"] == "12:00:00"  # unchanged

    # ── Update tests: combined changes ──

    async def test_update_start_time_and_loop_count_together(self, client, db_session):
        """Changing both start_time and loop_count should use new values for computation."""
        device_id, asset_id = await self._create_device_and_asset(db_session, duration=30.0)

        create = await client.post("/api/schedules", json={
            "name": "Combined Update",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "12:00",
        })
        sched_id = create.json()["id"]

        resp = await client.patch(f"/api/schedules/{sched_id}", json={
            "start_time": "15:00:00",
            "loop_count": 6,
        })
        assert resp.status_code == 200
        # 15:00 + 6×30s = 15:03:00
        assert resp.json()["end_time"] == "15:03:00"
        assert resp.json()["loop_count"] == 6

    # ── Persistence test ──

    async def test_loop_count_persists_through_list(self, client, db_session):
        device_id, asset_id = await self._create_device_and_asset(db_session)

        await client.post("/api/schedules", json={
            "name": "Persistent",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "loop_count": 7,
        })

        resp = await client.get("/api/schedules")
        assert resp.status_code == 200
        schedules = resp.json()
        match = [s for s in schedules if s["loop_count"] == 7]
        assert len(match) == 1
        # end_time should also have persisted: 08:00 + 7×30s = 08:03:30
        assert match[0]["end_time"] == "08:03:30"

    # ── Edge case: unrelated update doesn't recompute ──

    async def test_unrelated_update_preserves_end_time(self, client, db_session):
        """Updating name/priority/enabled should NOT recompute end_time."""
        device_id, asset_id = await self._create_device_and_asset(db_session)

        create = await client.post("/api/schedules", json={
            "name": "Won't Change End",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "08:00",
            "loop_count": 5,
        })
        sched_id = create.json()["id"]
        orig_end = create.json()["end_time"]  # 08:02:30

        resp = await client.patch(f"/api/schedules/{sched_id}", json={
            "name": "Renamed",
            "priority": 99,
            "enabled": False,
        })
        assert resp.status_code == 200
        assert resp.json()["end_time"] == orig_end
        assert resp.json()["name"] == "Renamed"
        assert resp.json()["priority"] == 99
