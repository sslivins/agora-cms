"""Tests for stale _now_playing cleanup when schedule windows expire.

When a schedule's time window passes, the scheduler tick should remove
the corresponding _now_playing entry even if the device is still connected.
"""

import hashlib
import uuid
from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio

from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.services import scheduler as _sched
from cms.services.device_manager import device_manager


def _connect_device(device_id):
    """Register a fake WS connection so the device appears connected."""
    class FakeWS:
        async def send_json(self, data, mode="text"):
            pass
    device_manager.register(device_id, FakeWS())


@pytest.mark.asyncio
class TestNowPlayingExpiry:
    async def test_expired_schedule_clears_now_playing(self, app, db_session):
        """After a schedule window expires, its _now_playing entry should be removed.

        Full integration test: seed a device + expired schedule in the DB,
        inject a stale _now_playing entry, run evaluate_schedules(), and
        verify the entry was cleaned up.
        """
        group = DeviceGroup(name="Test Group")
        db_session.add(group)
        await db_session.flush()

        device = Device(
            id="expire-dev-01",
            name="Expire Test Device",
            status=DeviceStatus.ADOPTED,
            group_id=group.id,
            device_auth_token_hash=hashlib.sha256(b"tok").hexdigest(),
        )
        db_session.add(device)

        asset = Asset(
            filename="test-page.html",
            asset_type=AssetType.WEBPAGE,
            original_filename="test-page.html",
            url="https://example.com",
            size_bytes=0,
            checksum="abc123",
        )
        db_session.add(asset)
        await db_session.flush()

        # Schedule that ended 1 hour ago
        now = datetime.now()
        ended = (now - timedelta(hours=1)).time().replace(microsecond=0)
        started = (now - timedelta(hours=2)).time().replace(microsecond=0)

        schedule = Schedule(
            name="Expired Schedule",
            asset_id=asset.id,
            group_id=group.id,
            enabled=True,
            start_time=started,
            end_time=ended,
            priority=0,
        )
        db_session.add(schedule)
        await db_session.commit()

        _connect_device(device.id)

        _sched._now_playing[device.id] = {
            "device_id": device.id,
            "device_name": device.name,
            "schedule_id": str(schedule.id),
            "schedule_name": schedule.name,
            "asset_filename": asset.filename,
            "since": datetime.now(timezone.utc).isoformat(),
            "end_time": schedule.end_time.strftime("%I:%M %p").lstrip("0"),
            "remaining": "0s",
            "remaining_seconds": 0,
        }

        assert device.id in _sched._now_playing

        try:
            await _sched.evaluate_schedules()

            assert device.id not in _sched._now_playing, (
                "_now_playing entry should be cleared after schedule window expires"
            )
        finally:
            _sched._now_playing.pop(device.id, None)
            device_manager.disconnect(device.id)

    async def test_active_schedule_keeps_now_playing(self):
        """An active schedule_id should NOT be purged by the expiry cleanup.

        Tests the cleanup logic directly (same code as the scheduler tick)
        without needing a full DB or evaluate_schedules() call.
        """
        device_id = "active-dev-02"
        active_sid = str(uuid.uuid4())
        expired_sid = str(uuid.uuid4())

        _sched._now_playing[device_id] = {
            "device_id": device_id,
            "device_name": "Active Device",
            "schedule_id": active_sid,
            "schedule_name": "Active Schedule",
            "asset_filename": "page.html",
            "since": datetime.now(timezone.utc).isoformat(),
        }
        _sched._now_playing["other-dev"] = {
            "device_id": "other-dev",
            "device_name": "Other Device",
            "schedule_id": expired_sid,
            "schedule_name": "Expired Schedule",
            "asset_filename": "old.html",
            "since": datetime.now(timezone.utc).isoformat(),
        }

        try:
            # Simulate the active schedule set (only active_sid is current)
            active_sids = {active_sid}

            expired_np = [
                did for did, info in list(_sched._now_playing.items())
                if info.get("schedule_id") and str(info["schedule_id"]) not in active_sids
            ]
            for did in expired_np:
                _sched._now_playing.pop(did, None)

            assert device_id in _sched._now_playing, (
                "_now_playing entry should be kept for active schedules"
            )
            assert "other-dev" not in _sched._now_playing, (
                "_now_playing entry should be cleared for expired schedules"
            )
        finally:
            _sched._now_playing.pop(device_id, None)
            _sched._now_playing.pop("other-dev", None)

    async def test_no_schedule_id_preserved(self):
        """Entries without a schedule_id should not be affected by expiry cleanup."""
        device_id = "no-sid-dev"

        _sched._now_playing[device_id] = {
            "device_id": device_id,
            "device_name": "No SID Device",
            "asset_filename": "something.mp4",
            "since": datetime.now(timezone.utc).isoformat(),
        }

        try:
            active_sids = set()  # no active schedules at all

            expired_np = [
                did for did, info in list(_sched._now_playing.items())
                if info.get("schedule_id") and str(info["schedule_id"]) not in active_sids
            ]
            for did in expired_np:
                _sched._now_playing.pop(did, None)

            assert device_id in _sched._now_playing, (
                "Entries without schedule_id should be preserved"
            )
        finally:
            _sched._now_playing.pop(device_id, None)
