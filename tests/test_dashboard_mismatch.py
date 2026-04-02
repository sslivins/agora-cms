"""Tests for dashboard schedule mismatch detection.

When a schedule is active for a device but the device is not actually playing
the expected asset, the dashboard should show a 'Not Playing' warning badge.
"""

import hashlib

import pytest
import pytest_asyncio
from cms.models.device import Device, DeviceStatus
from cms.services.device_manager import device_manager
from cms.services import scheduler as _sched


async def _seed_device(db_session, device_id="mismatch-01", name="Test Device"):
    device = Device(
        id=device_id,
        name=name,
        status=DeviceStatus.ADOPTED,
        device_auth_token_hash=hashlib.sha256(b"tok").hexdigest(),
    )
    db_session.add(device)
    await db_session.commit()
    return device


def _fake_connect(device_id, mode="splash", asset=None):
    class FakeWS:
        pass
    device_manager.register(device_id, FakeWS())
    device_manager.update_status(device_id, mode=mode, asset=asset)


def _seed_now_playing(device_id, device_name, schedule_name, asset_filename):
    _sched._now_playing[device_id] = {
        "device_id": device_id,
        "device_name": device_name,
        "schedule_id": "sched-1",
        "schedule_name": schedule_name,
        "asset_filename": asset_filename,
        "end_time": "5:00 PM",
        "remaining": "30 minutes",
        "remaining_seconds": 1800,
    }


@pytest.mark.asyncio
class TestDashboardMismatch:
    async def test_mismatch_shown_when_device_on_splash(self, app, db_session, client):
        """Device should be playing per schedule but is on splash → 'Not Playing' badge."""
        await _seed_device(db_session)
        _fake_connect("mismatch-01", mode="splash", asset=None)
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4")

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "Not Playing" in html
            assert "badge-missed" in html
            assert "card-playing-mismatch" in html
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)

    async def test_no_mismatch_when_playing_correct_asset(self, app, db_session, client):
        """Device is playing the scheduled asset → 'Playing' badge, no warning."""
        await _seed_device(db_session)
        _fake_connect("mismatch-01", mode="play", asset="sony-clip.mp4")
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4")

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "badge-started" in html
            assert "Not Playing" not in html
            assert "card-playing-mismatch" not in html
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)

    async def test_mismatch_when_playing_wrong_asset(self, app, db_session, client):
        """Device is playing a different asset than scheduled → mismatch warning."""
        await _seed_device(db_session)
        _fake_connect("mismatch-01", mode="play", asset="other-video.mp4")
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4")

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "Not Playing" in html
            assert "badge-missed" in html
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)

    async def test_mismatch_in_json_api(self, app, db_session, client):
        """The /api/dashboard JSON endpoint should include mismatch flag in now_playing."""
        await _seed_device(db_session)
        _fake_connect("mismatch-01", mode="splash", asset=None)
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4")

        try:
            resp = await client.get("/api/dashboard")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["now_playing"]) == 1
            assert data["now_playing"][0]["mismatch"] is True
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)

    async def test_no_mismatch_in_json_api(self, app, db_session, client):
        """JSON endpoint shows mismatch=False when device is playing correctly."""
        await _seed_device(db_session)
        _fake_connect("mismatch-01", mode="play", asset="sony-clip.mp4")
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4")

        try:
            resp = await client.get("/api/dashboard")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["now_playing"]) == 1
            assert data["now_playing"][0]["mismatch"] is False
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)
