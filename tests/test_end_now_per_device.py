"""Tests for per-device End Now skips (issue #240).

The End Now button on the dashboard should only affect the clicked device.
Prior behavior skipped the schedule for every device in the target group.

After the scheduler-state-dbback refactor, skip state lives exclusively in
the DB (``Schedule.skipped_until`` and ``ScheduleDeviceSkip``) — these
tests write skips directly to the database and assert that
:func:`build_device_sync` and :func:`load_skip_snapshot` honor them.
"""

from __future__ import annotations

from datetime import datetime, time

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.schedule_device_skip import ScheduleDeviceSkip
from cms.models.setting import CMSSetting
from cms.services import scheduler as sched_mod
from cms.services.scheduler import build_device_sync, load_skip_snapshot


@pytest.fixture(autouse=True)
def _reset_scheduler_state():
    """Clear the remaining replica-local scheduler caches between tests."""
    sched_mod._confirmed_playing.clear()
    sched_mod._last_sync_hash.clear()
    yield
    sched_mod._confirmed_playing.clear()
    sched_mod._last_sync_hash.clear()


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
        db.add(ScheduleDeviceSkip(
            schedule_id=sched.id,
            device_id=d1.id,
            skip_until=datetime(2099, 1, 1),
        ))
        await db.commit()

        sync_a = await build_device_sync(d1.id, db)
        sync_b = await build_device_sync(d2.id, db)

        assert sync_a is not None and len(sync_a.schedules) == 0
        assert sync_b is not None and len(sync_b.schedules) == 1
        assert sync_b.schedules[0].name == "Always"

    async def test_build_sync_honors_schedule_wide_skip(self, db):
        """Schedule-wide skip still removes schedule from every device."""
        sched, d1, d2 = await self._setup(db)
        sched.skipped_until = datetime(2099, 1, 1)
        await db.commit()

        sync_a = await build_device_sync(d1.id, db)
        sync_b = await build_device_sync(d2.id, db)

        assert len(sync_a.schedules) == 0
        assert len(sync_b.schedules) == 0

    async def test_load_skip_snapshot_reads_both_scopes(self, db):
        """load_skip_snapshot must surface both schedule-wide and per-device skips."""
        sched, d1, d2 = await self._setup(db)
        sched.skipped_until = datetime(2099, 1, 1)
        db.add(ScheduleDeviceSkip(
            schedule_id=sched.id,
            device_id=d2.id,
            skip_until=datetime(2099, 1, 1),
        ))
        await db.commit()

        snap = await load_skip_snapshot(db)
        assert str(sched.id) in snap.schedule_wide
        assert (str(sched.id), d2.id) in snap.per_device
        assert snap.is_schedule_skipped(str(sched.id))
        assert snap.is_skipped_for_device(str(sched.id), d2.id)
        # Schedule-wide skip also blocks d1 via is_skipped_for_device
        assert snap.is_skipped_for_device(str(sched.id), d1.id)

    async def test_active_as_of_drops_expired_entries(self, db):
        """Expired skips should not appear in the active-as-of view."""
        sched, d1, _ = await self._setup(db)
        sched.skipped_until = datetime(2000, 1, 1)  # long past
        db.add(ScheduleDeviceSkip(
            schedule_id=sched.id,
            device_id=d1.id,
            skip_until=datetime(2000, 1, 1),
        ))
        await db.commit()

        snap = await load_skip_snapshot(db)
        active = snap.active_as_of(datetime(2030, 1, 1))
        assert not active.is_schedule_skipped(str(sched.id))
        assert not active.is_skipped_for_device(str(sched.id), d1.id)
        # But expired_* still reports them for the purge loop
        assert str(sched.id) in snap.expired_schedule_ids(datetime(2030, 1, 1))
        assert (str(sched.id), d1.id) in snap.expired_device_pairs(datetime(2030, 1, 1))
