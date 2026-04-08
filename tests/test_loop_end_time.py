"""Tests: auto-compute end_time when loop_count is provided."""

import pytest


@pytest.mark.asyncio
class TestLoopEndTimeComputation:
    """When loop_count is set, the server should compute end_time from
    start_time + (loop_count × asset duration_seconds)."""

    async def _create_device_and_asset(self, db_session, duration_seconds=634.0):
        """Helper: create an adopted device and a video asset with known duration."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        device = Device(id="loop-pi", name="Loop Test", status=DeviceStatus.ADOPTED)
        asset = Asset(
            filename="bbb.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=100_000,
            checksum="abc123",
            duration_seconds=duration_seconds,
        )
        db_session.add_all([device, asset])
        await db_session.commit()
        return device.id, str(asset.id)

    async def test_end_time_computed_when_loop_count_set(self, client, db_session):
        """With loop_count=1 and a 634s asset, end_time should be start + 634s."""
        device_id, asset_id = await self._create_device_and_asset(db_session, duration_seconds=634.0)

        resp = await client.post("/api/schedules", json={
            "name": "Auto End",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "10:00:00",
            "loop_count": 1,
        })
        assert resp.status_code == 201
        data = resp.json()
        # 634s = 10 min 34 s → end_time = 10:10:34
        assert data["end_time"] == "10:10:34"
        assert data["loop_count"] == 1

    async def test_end_time_computed_multiple_loops(self, client, db_session):
        """With loop_count=3 and a 600s (10 min) asset, end_time = start + 1800s."""
        device_id, asset_id = await self._create_device_and_asset(db_session, duration_seconds=600.0)

        resp = await client.post("/api/schedules", json={
            "name": "Triple Loop",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "14:00:00",
            "loop_count": 3,
        })
        assert resp.status_code == 201
        data = resp.json()
        # 3 × 600s = 1800s = 30 min → end_time = 14:30:00
        assert data["end_time"] == "14:30:00"

    async def test_end_time_overridden_when_loop_count_set(self, client, db_session):
        """If both end_time and loop_count are provided, computed value wins."""
        device_id, asset_id = await self._create_device_and_asset(db_session, duration_seconds=600.0)

        resp = await client.post("/api/schedules", json={
            "name": "Override End",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "10:00:00",
            "end_time": "23:00:00",  # user-provided, should be overridden
            "loop_count": 1,
        })
        assert resp.status_code == 201
        data = resp.json()
        # 1 × 600s = 10 min → end_time = 10:10:00 (not 23:00:00)
        assert data["end_time"] == "10:10:00"

    async def test_end_time_still_required_without_loop_count(self, client, db_session):
        """Without loop_count, end_time is still required."""
        device_id, asset_id = await self._create_device_and_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "No End Time",
            "device_id": device_id,
            "asset_id": asset_id,
            "start_time": "10:00:00",
        })
        assert resp.status_code == 422  # validation error

    async def test_no_duration_requires_end_time(self, client, db_session):
        """If asset has no duration (e.g. image), end_time is required even with loop_count."""
        from cms.models.asset import Asset, AssetType
        from cms.models.device import Device, DeviceStatus

        device = Device(id="img-pi", name="Image Test", status=DeviceStatus.ADOPTED)
        asset = Asset(
            filename="splash.png",
            asset_type=AssetType.IMAGE,
            size_bytes=50_000,
            checksum="def456",
            duration_seconds=None,  # no duration
        )
        db_session.add_all([device, asset])
        await db_session.commit()

        resp = await client.post("/api/schedules", json={
            "name": "Image Loop",
            "device_id": device.id,
            "asset_id": str(asset.id),
            "start_time": "10:00:00",
            "loop_count": 5,
        })
        # Should fail because we can't compute end_time without duration
        assert resp.status_code == 422
