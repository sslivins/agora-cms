"""Tests for dashboard schedule mismatch and device-offline detection.

When a schedule is active for a device but the device is not actually playing
the expected asset, the dashboard should show a 'Not Playing' warning badge.
When the target device is offline, the dashboard should show a 'Device Offline'
badge instead of 'Starting…' or 'Not Playing'.
"""

import hashlib
import uuid
from datetime import datetime, time, timedelta, timezone

import pytest
import pytest_asyncio
from cms.models.device import Device, DeviceStatus
from cms.services.device_manager import device_manager
from cms.services import device_presence
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


async def _seed_schedule(db_session, device_id="mismatch-01",
                         asset_filename="sony-clip.mp4",
                         schedule_name="Sony Schedule"):
    from shared.models.asset import Asset, AssetType
    from cms.models.device import DeviceGroup
    from cms.models.schedule import Schedule
    from sqlalchemy import update

    asset = Asset(id=uuid.uuid4(), filename=asset_filename,
                  asset_type=AssetType.VIDEO, checksum="abc123")
    db_session.add(asset)

    group = DeviceGroup(id=uuid.uuid4(), name=f"Test Group {uuid.uuid4().hex[:6]}")
    db_session.add(group)
    await db_session.flush()

    await db_session.execute(
        update(Device).where(Device.id == device_id).values(group_id=group.id)
    )

    schedule = Schedule(
        id=uuid.uuid4(),
        name=schedule_name,
        asset_id=asset.id,
        group_id=group.id,
        start_time=time(0, 0, 0),
        end_time=time(23, 59, 59),
        enabled=True,
        priority=0,
    )
    db_session.add(schedule)
    await db_session.commit()
    return schedule, asset, group


async def _fake_connect(db_session, device_id, mode="splash", asset=None):
    """Register a fake connection AND persist presence/state in the DB.

    Mirrors what ``/ws/device`` does in Stage 2c — the in-memory
    registry keeps the socket reference, but every reader hits Postgres.
    """
    class FakeWS:
        pass
    device_manager.register(device_id, FakeWS())
    from cms.services import device_presence
    await device_presence.mark_online(db_session, device_id)
    await device_presence.update_status(
        db_session, device_id, {"mode": mode, "asset": asset},
    )


async def _fake_disconnect(db_session, device_id):
    device_manager.disconnect(device_id)
    from cms.services import device_presence
    await device_presence.mark_offline(db_session, device_id)


def _seed_confirmed(device_id, schedule_id, since=None):
    if since is None:
        since = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
    _sched._confirmed_playing[device_id] = {
        "schedule_id": str(schedule_id),
        "since": since,
    }


@pytest.mark.asyncio
class TestDashboardMismatch:
    async def test_mismatch_shown_when_device_on_splash(self, app, db_session, client):
        """Device should be playing per schedule but is on splash → 'Not Playing' badge."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        await _fake_connect(db_session, "mismatch-01", mode="splash", asset=None)
        _seed_confirmed("mismatch-01", schedule.id)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "Not Playing" in html
            assert "badge-missed" in html
            assert "card-playing-mismatch" in html
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_no_mismatch_when_playing_correct_asset(self, app, db_session, client):
        """Device is playing the scheduled asset → 'Playing' badge, no warning."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        await _fake_connect(db_session, "mismatch-01", mode="play", asset="sony-clip.mp4")
        _seed_confirmed("mismatch-01", schedule.id)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "badge-started" in html
            assert "Not Playing" not in html
            assert "card-playing-mismatch" not in html
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_mismatch_when_playing_wrong_asset(self, app, db_session, client):
        """Device is playing a different asset than scheduled → mismatch warning."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        await _fake_connect(db_session, "mismatch-01", mode="play", asset="other-video.mp4")
        _seed_confirmed("mismatch-01", schedule.id)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "Not Playing" in html
            assert "badge-missed" in html
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_mismatch_in_json_api(self, app, db_session, client):
        """The /api/dashboard JSON endpoint should include mismatch flag in now_playing."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        await _fake_connect(db_session, "mismatch-01", mode="splash", asset=None)
        _seed_confirmed("mismatch-01", schedule.id)

        try:
            resp = await client.get("/api/dashboard")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["now_playing"]) == 1
            assert data["now_playing"][0]["mismatch"] is True
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_no_mismatch_in_json_api(self, app, db_session, client):
        """JSON endpoint shows mismatch=False when device is playing correctly."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        await _fake_connect(db_session, "mismatch-01", mode="play", asset="sony-clip.mp4")
        _seed_confirmed("mismatch-01", schedule.id)

        try:
            resp = await client.get("/api/dashboard")
            assert resp.status_code == 200
            data = resp.json()
            assert len(data["now_playing"]) == 1
            assert data["now_playing"][0]["mismatch"] is False
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)


@pytest.mark.asyncio
class TestDashboardStartingGrace:
    """Grace period: show 'Starting...' instead of 'Not Playing' for ~45s after activation."""

    async def test_starting_badge_within_grace_period(self, app, db_session, client):
        """Within 45s of activation, a mismatched device shows 'Starting...' not 'Not Playing'."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        await _fake_connect(db_session, "mismatch-01", mode="splash", asset=None)
        since = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        _seed_confirmed("mismatch-01", schedule.id, since=since)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "Starting" in html
            assert "badge-processing" in html
            assert "Not Playing" not in html
            assert "badge-missed" not in html
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_mismatch_after_grace_period(self, app, db_session, client):
        """After 45s, a mismatched device shows 'Not Playing' as before."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        await _fake_connect(db_session, "mismatch-01", mode="splash", asset=None)
        since = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        _seed_confirmed("mismatch-01", schedule.id, since=since)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "Not Playing" in html
            assert "badge-missed" in html
            assert "Starting" not in html
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_starting_in_json_api(self, app, db_session, client):
        """JSON endpoint returns starting=True during the grace period."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        await _fake_connect(db_session, "mismatch-01", mode="splash", asset=None)
        since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        _seed_confirmed("mismatch-01", schedule.id, since=since)

        try:
            resp = await client.get("/api/dashboard")
            assert resp.status_code == 200
            data = resp.json()
            np = data["now_playing"][0]
            assert np["mismatch"] is False
            assert np["starting"] is True
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_no_starting_when_playing_correctly(self, app, db_session, client):
        """A device playing the correct asset should show 'Playing', not 'Starting...'."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        await _fake_connect(db_session, "mismatch-01", mode="play", asset="sony-clip.mp4")
        since = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        _seed_confirmed("mismatch-01", schedule.id, since=since)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "badge-started" in html
            assert "Starting" not in html
            assert "Not Playing" not in html
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)

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
        schedule, asset, group = await _seed_schedule(db_session)
        since = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        _seed_confirmed("mismatch-01", schedule.id, since=since)

        try:
            # 1) Device not playing yet → "Starting…" during grace period
            await _fake_connect(db_session, "mismatch-01", mode="splash", asset=None)
            resp1 = await client.get("/api/dashboard")
            np1 = resp1.json()["now_playing"][0]
            assert np1["starting"] is True
            assert np1["mismatch"] is False

            # 2) Device starts playing the correct asset
            await device_presence.update_status(db_session, "mismatch-01", {"mode": "play", "asset": "sony-clip.mp4"})

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
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_no_starting_when_playing_different_asset(self, app, db_session, client):
        """When device is playing (a different asset due to preemption), don't
        show 'Starting...' — the device is working fine, _now_playing is just stale."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        await _fake_connect(db_session, "mismatch-01", mode="play", asset="other-video.mp4")
        since = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        _seed_confirmed("mismatch-01", schedule.id, since=since)

        try:
            resp = await client.get("/api/dashboard")
            np = resp.json()["now_playing"][0]
            assert np["mismatch"] is False
            assert np.get("starting") is not True, (
                "starting should not be set when device is actively playing"
            )
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_no_starting_when_playing_different_asset_html(self, app, db_session, client):
        """HTML dashboard should not show Starting badge when device plays a different asset."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        await _fake_connect(db_session, "mismatch-01", mode="play", asset="other-video.mp4")
        since = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        _seed_confirmed("mismatch-01", schedule.id, since=since)

        try:
            resp = await client.get("/")
            html = resp.text
            assert "Starting" not in html
            assert "badge-processing" not in html
            assert "badge-started" in html
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)


@pytest.mark.asyncio
class TestDashboardDeviceOffline:
    """When a scheduled device is offline, the dashboard should show 'Device Offline'
    instead of 'Starting…' or 'Not Playing'."""

    async def test_offline_badge_in_now_playing_html(self, app, db_session, client):
        """An offline device in now_playing shows 'Device Offline' badge, not 'Not Playing'."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        # Do NOT call _fake_connect — device stays offline
        since = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        _seed_confirmed("mismatch-01", schedule.id, since=since)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "Device Offline" in html
            assert "badge-offline" in html
            assert "Not Playing" not in html
            assert "badge-missed" not in html
            assert "Starting" not in html
        finally:
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_offline_badge_in_now_playing_json(self, app, db_session, client):
        """JSON endpoint returns device_offline=True for offline devices."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        since = (datetime.now(timezone.utc) - timedelta(seconds=60)).isoformat()
        _seed_confirmed("mismatch-01", schedule.id, since=since)

        try:
            resp = await client.get("/api/dashboard")
            assert resp.status_code == 200
            np = resp.json()["now_playing"][0]
            assert np.get("device_offline") is True
            assert np["mismatch"] is False
            assert np.get("starting") is not True
        finally:
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_offline_badge_during_grace_period(self, app, db_session, client):
        """Within the 45s grace period, an offline device shows 'Device Offline' not 'Starting…'."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        since = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        _seed_confirmed("mismatch-01", schedule.id, since=since)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "Device Offline" in html
            assert "badge-offline" in html
            assert "Starting" not in html
            assert "badge-processing" not in html
        finally:
            _sched._confirmed_playing.pop("mismatch-01", None)

    async def test_offline_to_online_transition(self, app, db_session, client):
        """When an offline device reconnects, the badge should transition from
        'Device Offline' to 'Starting…' and then to 'Playing'."""
        await _seed_device(db_session)
        schedule, asset, group = await _seed_schedule(db_session)
        since = (datetime.now(timezone.utc) - timedelta(seconds=10)).isoformat()
        _seed_confirmed("mismatch-01", schedule.id, since=since)

        try:
            # 1) Device offline → "Device Offline" badge
            resp1 = await client.get("/api/dashboard")
            np1 = resp1.json()["now_playing"][0]
            assert np1.get("device_offline") is True

            # 2) Device comes online (not playing yet) → "Starting…"
            await _fake_connect(db_session, "mismatch-01", mode="splash", asset=None)
            resp2 = await client.get("/api/dashboard")
            np2 = resp2.json()["now_playing"][0]
            assert np2.get("device_offline") is not True
            assert np2.get("starting") is True

            # 3) Device starts playing → "Playing"
            await device_presence.update_status(db_session, "mismatch-01", {"mode": "play", "asset": "sony-clip.mp4"})
            resp3 = await client.get("/api/dashboard")
            np3 = resp3.json()["now_playing"][0]
            assert np3.get("device_offline") is not True
            assert np3.get("starting") is not True
            assert np3["mismatch"] is False
        finally:
            await _fake_disconnect(db_session, "mismatch-01")
            _sched._confirmed_playing.pop("mismatch-01", None)
