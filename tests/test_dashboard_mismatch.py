"""Tests for dashboard schedule mismatch detection.

When a schedule is active for a device but the device is not actually playing
the expected asset, the dashboard should show a 'Not Playing' warning badge.
"""

import hashlib
from datetime import datetime, timedelta, timezone

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


def _seed_now_playing(device_id, device_name, schedule_name, asset_filename, since=None):
    if since is None:
        # Default to 60s ago — outside the 45s grace period so existing
        # mismatch tests keep their "Not Playing" behaviour.
        since = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    _sched._now_playing[device_id] = {
        "device_id": device_id,
        "device_name": device_name,
        "schedule_id": "sched-1",
        "schedule_name": schedule_name,
        "asset_filename": asset_filename,
        "since": since or datetime.now(timezone.utc).isoformat(),
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


@pytest.mark.asyncio
class TestDashboardStartingGrace:
    """Grace period: show 'Starting...' instead of 'Not Playing' for ~45s after activation."""

    async def test_starting_badge_within_grace_period(self, app, db_session, client):
        """Within 45s of activation, a mismatched device shows 'Starting...' not 'Not Playing'."""
        await _seed_device(db_session)
        _fake_connect("mismatch-01", mode="splash", asset=None)
        since = datetime.now(timezone.utc) - timedelta(seconds=10)
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4",
                          since=since.isoformat())

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "Starting" in html
            assert "badge-processing" in html
            assert "Not Playing" not in html
            assert "badge-missed" not in html
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)

    async def test_mismatch_after_grace_period(self, app, db_session, client):
        """After 45s, a mismatched device shows 'Not Playing' as before."""
        await _seed_device(db_session)
        _fake_connect("mismatch-01", mode="splash", asset=None)
        since = datetime.now(timezone.utc) - timedelta(seconds=60)
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4",
                          since=since.isoformat())

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "Not Playing" in html
            assert "badge-missed" in html
            assert "Starting" not in html
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)

    async def test_starting_in_json_api(self, app, db_session, client):
        """JSON endpoint returns starting=True during the grace period."""
        await _seed_device(db_session)
        _fake_connect("mismatch-01", mode="splash", asset=None)
        since = datetime.now(timezone.utc) - timedelta(seconds=5)
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4",
                          since=since.isoformat())

        try:
            resp = await client.get("/api/dashboard")
            assert resp.status_code == 200
            data = resp.json()
            np = data["now_playing"][0]
            assert np["mismatch"] is False
            assert np["starting"] is True
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)

    async def test_no_starting_when_playing_correctly(self, app, db_session, client):
        """A device playing the correct asset should show 'Playing', not 'Starting...'."""
        await _seed_device(db_session)
        _fake_connect("mismatch-01", mode="play", asset="sony-clip.mp4")
        since = datetime.now(timezone.utc) - timedelta(seconds=5)
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4",
                          since=since.isoformat())

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "badge-started" in html
            assert "Starting" not in html
            assert "Not Playing" not in html
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)

    async def test_starting_clears_after_device_begins_playing(self, app, db_session, client):
        """Starting badge must clear once the device transitions to playing.

        Regression: get_now_playing() returned references to the global
        _now_playing dicts.  The dashboard route set starting=True on the
        shared dict during the grace period.  On subsequent requests — after
        the device began playing correctly — mismatch was False so the
        ``if np["mismatch"]`` block was skipped, leaving starting=True from
        the previous request.  The fingerprint never changed and the page
        stayed stuck on 'Starting…'.
        """
        await _seed_device(db_session)
        since = datetime.now(timezone.utc) - timedelta(seconds=10)
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4",
                          since=since.isoformat())

        try:
            # 1) Device not playing yet → "Starting…" during grace period
            _fake_connect("mismatch-01", mode="splash", asset=None)
            resp1 = await client.get("/api/dashboard")
            np1 = resp1.json()["now_playing"][0]
            assert np1["starting"] is True
            assert np1["mismatch"] is False

            # 2) Device starts playing the correct asset
            device_manager.update_status("mismatch-01", mode="play", asset="sony-clip.mp4")

            resp2 = await client.get("/api/dashboard")
            np2 = resp2.json()["now_playing"][0]
            # Must show Playing, not Starting
            assert np2.get("starting") is not True, (
                "starting flag stuck from previous request — "
                "get_now_playing() must return copies"
            )
            assert np2["mismatch"] is False

            # 3) Full page should show 'Playing' badge, not 'Starting…'
            resp3 = await client.get("/")
            html = resp3.text
            assert "badge-started" in html
            assert "Starting" not in html
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)

    async def test_no_starting_when_playing_different_asset(self, app, db_session, client):
        """When device is playing (a different asset due to preemption), don't
        show 'Starting...' — the device is working fine, _now_playing is just stale."""
        await _seed_device(db_session)
        _fake_connect("mismatch-01", mode="play", asset="other-video.mp4")
        since = datetime.now(timezone.utc) - timedelta(seconds=10)
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4",
                          since=since.isoformat())

        try:
            resp = await client.get("/api/dashboard")
            np = resp.json()["now_playing"][0]
            assert np["mismatch"] is False
            assert np.get("starting") is not True, (
                "starting should not be set when device is actively playing"
            )
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)

    async def test_no_starting_when_playing_different_asset_html(self, app, db_session, client):
        """HTML dashboard should not show Starting badge when device plays a different asset."""
        await _seed_device(db_session)
        _fake_connect("mismatch-01", mode="play", asset="other-video.mp4")
        since = datetime.now(timezone.utc) - timedelta(seconds=10)
        _seed_now_playing("mismatch-01", "Test Device", "Sony Schedule", "sony-clip.mp4",
                          since=since.isoformat())

        try:
            resp = await client.get("/")
            html = resp.text
            assert "Starting" not in html
            assert "badge-processing" not in html
            assert "badge-started" in html
        finally:
            device_manager.disconnect("mismatch-01")
            _sched._now_playing.pop("mismatch-01", None)
