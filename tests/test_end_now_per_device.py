"""Tests for per-device End Now skips (issue #240).

The End Now button on the dashboard should only affect the clicked device.
Prior behavior skipped the schedule for every device in the target group.
"""

from __future__ import annotations

import uuid
from datetime import datetime, time, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.schedule_device_skip import ScheduleDeviceSkip
from cms.models.setting import CMSSetting
from cms.services import scheduler as sched_mod
from cms.services.scheduler import (
    build_device_sync,
    clear_schedule_skip,
    is_schedule_skipped_for_device,
    skip_schedule_until,
)


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    """Per-test isolation for module-level skip dicts."""
    sched_mod._skipped.clear()
    sched_mod._device_skipped.clear()
    sched_mod._skipped_loaded = False
    yield
    sched_mod._skipped.clear()
    sched_mod._device_skipped.clear()
    sched_mod._skipped_loaded = False


class TestInMemorySkipHelpers:
    def test_schedule_wide_skip(self):
        sid = str(uuid.uuid4())
        until = datetime(2030, 1, 1, 12, 0)
        skip_schedule_until(sid, until)
        assert is_schedule_skipped_for_device(sid, "dev-a")
        assert is_schedule_skipped_for_device(sid, "dev-b")

    def test_per_device_skip_isolation(self):
        sid = str(uuid.uuid4())
        until = datetime(2030, 1, 1, 12, 0)
        skip_schedule_until(sid, until, device_id="dev-a")
        assert is_schedule_skipped_for_device(sid, "dev-a")
        assert not is_schedule_skipped_for_device(sid, "dev-b")

    def test_schedule_wide_overrides_per_device(self):
        sid = str(uuid.uuid4())
        until = datetime(2030, 1, 1, 12, 0)
        skip_schedule_until(sid, until, device_id="dev-a")
        skip_schedule_until(sid, until)  # schedule-wide
        assert is_schedule_skipped_for_device(sid, "dev-a")
        assert is_schedule_skipped_for_device(sid, "dev-b")

    def test_clear_device_skip_only(self):
        sid = str(uuid.uuid4())
        until = datetime(2030, 1, 1, 12, 0)
        skip_schedule_until(sid, until, device_id="dev-a")
        skip_schedule_until(sid, until, device_id="dev-b")
        clear_schedule_skip(sid, device_id="dev-a")
        assert not is_schedule_skipped_for_device(sid, "dev-a")
        assert is_schedule_skipped_for_device(sid, "dev-b")

    def test_clear_all_drops_per_device_skips(self):
        sid = str(uuid.uuid4())
        until = datetime(2030, 1, 1, 12, 0)
        skip_schedule_until(sid, until, device_id="dev-a")
        skip_schedule_until(sid, until, device_id="dev-b")
        clear_schedule_skip(sid)
        assert not is_schedule_skipped_for_device(sid, "dev-a")
        assert not is_schedule_skipped_for_device(sid, "dev-b")

    def test_per_device_skip_clears_only_that_confirmed_playing(self):
        sid = str(uuid.uuid4())
        sched_mod._confirmed_playing["dev-a"] = {"schedule_id": sid, "since": "t"}
        sched_mod._confirmed_playing["dev-b"] = {"schedule_id": sid, "since": "t"}
        skip_schedule_until(sid, datetime(2030, 1, 1, 12, 0), device_id="dev-a")
        assert "dev-a" not in sched_mod._confirmed_playing
        assert "dev-b" in sched_mod._confirmed_playing
        sched_mod._confirmed_playing.clear()

    def test_schedule_wide_skip_clears_all_confirmed_playing(self):
        sid = str(uuid.uuid4())
        sched_mod._confirmed_playing["dev-a"] = {"schedule_id": sid, "since": "t"}
        sched_mod._confirmed_playing["dev-b"] = {"schedule_id": sid, "since": "t"}
        skip_schedule_until(sid, datetime(2030, 1, 1, 12, 0))
        assert "dev-a" not in sched_mod._confirmed_playing
        assert "dev-b" not in sched_mod._confirmed_playing
        sched_mod._confirmed_playing.clear()


@pytest.mark.asyncio
class TestPerDeviceSkipAgainstDB:
    @pytest_asyncio.fixture
    async def db(self, db_engine):
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        async with factory() as session:
            yield session

    async def _setup(self, db):
        """Two devices in one group sharing one schedule."""
        db.add(CMSSetting(key="timezone", value="UTC"))
        asset = Asset(
            filename="v.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1, checksum="c",
        )
        group = DeviceGroup(name="Lobby")
        db.add_all([asset, group])
        await db.flush()
        d1 = Device(id="pi-240-a", name="A", status=DeviceStatus.ADOPTED, group_id=group.id)
        d2 = Device(id="pi-240-b", name="B", status=DeviceStatus.ADOPTED, group_id=group.id)
        db.add_all([d1, d2])
        sched = Schedule(
            name="Always",
            group_id=group.id,
            asset_id=asset.id,
            start_time=time(0, 0),
            end_time=time(23, 59, 59),
        )
        db.add(sched)
        await db.commit()
        return sched, d1, d2

    async def test_build_sync_drops_schedule_only_for_skipped_device(self, db):
        """Skipping for device A must not affect device B's sync."""
        sched, d1, d2 = await self._setup(db)
        skip_schedule_until(str(sched.id), datetime(2099, 1, 1), device_id=d1.id)

        sync_a = await build_device_sync(d1.id, db)
        sync_b = await build_device_sync(d2.id, db)

        assert sync_a is not None and len(sync_a.schedules) == 0
        assert sync_b is not None and len(sync_b.schedules) == 1
        assert sync_b.schedules[0].name == "Always"

    async def test_build_sync_honors_schedule_wide_skip(self, db):
        """Schedule-wide skip still removes schedule from every device."""
        sched, d1, d2 = await self._setup(db)
        skip_schedule_until(str(sched.id), datetime(2099, 1, 1))

        sync_a = await build_device_sync(d1.id, db)
        sync_b = await build_device_sync(d2.id, db)

        assert len(sync_a.schedules) == 0
        assert len(sync_b.schedules) == 0

    async def test_persisted_per_device_skip_is_loaded_on_first_use(self, db):
        """_ensure_skips_loaded picks up schedule_device_skips rows."""
        sched, d1, d2 = await self._setup(db)
        db.add(ScheduleDeviceSkip(
            schedule_id=sched.id,
            device_id=d1.id,
            skip_until=datetime(2099, 1, 1),
        ))
        await db.commit()

        # Simulate cold boot: wipe in-memory, then trigger a build that loads
        sched_mod._device_skipped.clear()
        sched_mod._skipped_loaded = False

        # Force the load and verify it populated the dict
        await sched_mod._ensure_skips_loaded(db)
        assert (str(sched.id), d1.id) in sched_mod._device_skipped, (
            f"expected ({str(sched.id)!r}, {d1.id!r}) in {sched_mod._device_skipped}"
        )

        sync_a = await build_device_sync(d1.id, db)
        sync_b = await build_device_sync(d2.id, db)

        assert len(sync_a.schedules) == 0
        assert len(sync_b.schedules) == 1
