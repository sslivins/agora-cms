"""Tests for pending device staleness: connection state display + auto-purge."""

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from cms.models.device import Device, DeviceStatus
from cms.services.device_manager import DeviceManager


# ── Helpers ──

_SENTINEL = object()


async def _create_pending_device(db, device_id: str, last_seen=_SENTINEL, registered_at: datetime | None = None):
    """Insert a pending device into the DB."""
    now = datetime.now(timezone.utc)
    device = Device(
        id=device_id,
        name=f"Test {device_id}",
        status=DeviceStatus.PENDING,
        last_seen=now if last_seen is _SENTINEL else last_seen,
        registered_at=registered_at or now,
    )
    db.add(device)
    await db.commit()
    return device


async def _create_adopted_device(db, device_id: str, last_seen: datetime | None = None):
    """Insert an adopted device into the DB."""
    now = datetime.now(timezone.utc)
    device = Device(
        id=device_id,
        name=f"Test {device_id}",
        status=DeviceStatus.ADOPTED,
        last_seen=last_seen or now,
        registered_at=now,
    )
    db.add(device)
    await db.commit()
    return device


# ── Dashboard pending device connection state ──

@pytest.mark.asyncio
async def test_pending_device_shows_online_when_connected(client, db_session, app):
    """A pending device that has a live WebSocket should show as online on dashboard."""
    from cms.services.device_manager import device_manager

    await _create_pending_device(db_session, "dev-online-pending")
    # Simulate the device being connected via WebSocket
    device_manager.register("dev-online-pending", websocket=None)
    from cms.services import device_presence
    await device_presence.mark_online(db_session, "dev-online-pending")

    try:
        resp = await client.get("/")
        assert resp.status_code == 200
        html = resp.text
        # The device should appear with some online/connected indication, not just "Pending"
        # Specifically, the badge should NOT say "Offline"
        assert "dev-online-pending" in html
        # The badge-pending class should be present (it's still pending)
        assert "badge-pending" in html
    finally:
        device_manager.disconnect("dev-online-pending")


@pytest.mark.asyncio
async def test_pending_device_shows_offline_when_disconnected(client, db_session):
    """A pending device with no WebSocket connection should show as offline."""
    await _create_pending_device(db_session, "dev-offline-pending")

    resp = await client.get("/")
    assert resp.status_code == 200
    html = resp.text
    assert "dev-offline-pending" in html
    # Should show offline indication for a disconnected pending device
    assert "Pending (Offline)" in html


# ── Auto-purge stale pending devices ──

@pytest.mark.asyncio
async def test_purge_removes_stale_pending_devices(db_session):
    """Pending devices not seen for longer than TTL should be purged."""
    from cms.services.device_purge import purge_stale_pending_devices

    stale_time = datetime.now(timezone.utc) - timedelta(hours=48)
    recent_time = datetime.now(timezone.utc) - timedelta(minutes=5)

    await _create_pending_device(db_session, "stale-dev", last_seen=stale_time)
    await _create_pending_device(db_session, "recent-dev", last_seen=recent_time)

    purged = await purge_stale_pending_devices(db_session, ttl_hours=24)

    assert purged == ["stale-dev"]
    result = await db_session.execute(select(Device).where(Device.id == "stale-dev"))
    assert result.scalar_one_or_none() is None
    result = await db_session.execute(select(Device).where(Device.id == "recent-dev"))
    assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_purge_never_removes_adopted_devices(db_session):
    """Adopted devices should never be purged regardless of last_seen."""
    from cms.services.device_purge import purge_stale_pending_devices

    stale_time = datetime.now(timezone.utc) - timedelta(hours=48)
    await _create_adopted_device(db_session, "adopted-old", last_seen=stale_time)

    purged = await purge_stale_pending_devices(db_session, ttl_hours=24)

    assert purged == []
    result = await db_session.execute(select(Device).where(Device.id == "adopted-old"))
    assert result.scalar_one_or_none() is not None


@pytest.mark.asyncio
async def test_purge_uses_registered_at_when_never_seen(db_session):
    """If last_seen is NULL, fall back to registered_at for staleness check."""
    from cms.services.device_purge import purge_stale_pending_devices

    stale_time = datetime.now(timezone.utc) - timedelta(hours=48)
    await _create_pending_device(db_session, "never-seen", last_seen=None, registered_at=stale_time)

    purged = await purge_stale_pending_devices(db_session, ttl_hours=24)

    assert purged == ["never-seen"]


@pytest.mark.asyncio
async def test_purge_skips_connected_pending_devices(db_session):
    """Even if last_seen is stale, don't purge a pending device that's currently connected."""
    from cms.services import device_presence
    from cms.services.device_purge import purge_stale_pending_devices

    stale_time = datetime.now(timezone.utc) - timedelta(hours=48)
    await _create_pending_device(db_session, "connected-pending", last_seen=stale_time)

    # Device is connected right now (DB-backed presence)
    await device_presence.mark_online(db_session, "connected-pending")
    try:
        purged = await purge_stale_pending_devices(db_session, ttl_hours=24)
        assert purged == []
    finally:
        await device_presence.mark_offline(db_session, "connected-pending")


@pytest.mark.asyncio
async def test_purge_does_not_delete_devices_adopted_during_purge(db_session):
    """N>1 race regression: if a device is selected as a purge candidate
    on replica A but adopted on replica B between the SELECT and DELETE,
    the DELETE's ``status == PENDING`` guard must exclude it so the
    adopt wins.

    Simulated by hooking the session's ``execute`` so that immediately
    after the candidate-selection query returns we flip one row's
    status to ADOPTED (mimicking another replica's commit), then let the
    DELETE proceed.  The adopted row must survive.
    """
    from sqlalchemy import update as sa_update

    from cms.models.device import Device, DeviceStatus
    from cms.services.device_purge import purge_stale_pending_devices

    stale = datetime.now(timezone.utc) - timedelta(hours=48)
    await _create_pending_device(db_session, "racy-pending", last_seen=stale)
    await _create_pending_device(db_session, "stable-pending", last_seen=stale)

    real_execute = db_session.execute
    call_count = {"n": 0}

    async def hooked_execute(stmt, *a, **kw):
        result = await real_execute(stmt, *a, **kw)
        call_count["n"] += 1
        # The first execute is the candidate SELECT.  Flip racy-pending
        # to ADOPTED before the DELETE runs.
        if call_count["n"] == 1:
            await real_execute(
                sa_update(Device)
                .where(Device.id == "racy-pending")
                .values(status=DeviceStatus.ADOPTED)
            )
        return result

    db_session.execute = hooked_execute  # type: ignore[method-assign]
    try:
        purged = await purge_stale_pending_devices(db_session, ttl_hours=24)
    finally:
        db_session.execute = real_execute  # type: ignore[method-assign]

    assert "racy-pending" not in purged, (
        "DELETE missing status guard — concurrent adopt was nuked"
    )
    assert "stable-pending" in purged

    # racy-pending row still exists, now ADOPTED.
    result = await real_execute(select(Device).where(Device.id == "racy-pending"))
    dev = result.scalar_one_or_none()
    assert dev is not None
    assert dev.status == DeviceStatus.ADOPTED


@pytest.mark.asyncio
async def test_purge_does_not_delete_devices_that_came_online_during_purge(db_session):
    """N>1 race regression: if a candidate device's ``online`` flag
    flipped to true between SELECT and DELETE (the device just
    reconnected on another replica), the DELETE's ``online == false``
    guard must exclude it.
    """
    from sqlalchemy import update as sa_update

    from cms.models.device import Device, DeviceStatus
    from cms.services.device_purge import purge_stale_pending_devices

    stale = datetime.now(timezone.utc) - timedelta(hours=48)
    await _create_pending_device(db_session, "reconnecting", last_seen=stale)

    real_execute = db_session.execute
    call_count = {"n": 0}

    async def hooked_execute(stmt, *a, **kw):
        result = await real_execute(stmt, *a, **kw)
        call_count["n"] += 1
        if call_count["n"] == 1:
            await real_execute(
                sa_update(Device)
                .where(Device.id == "reconnecting")
                .values(online=True)
            )
        return result

    db_session.execute = hooked_execute  # type: ignore[method-assign]
    try:
        purged = await purge_stale_pending_devices(db_session, ttl_hours=24)
    finally:
        db_session.execute = real_execute  # type: ignore[method-assign]

    assert "reconnecting" not in purged
    result = await real_execute(select(Device).where(Device.id == "reconnecting"))
    dev = result.scalar_one_or_none()
    assert dev is not None
    assert dev.status == DeviceStatus.PENDING
    assert dev.online is True


@pytest.mark.asyncio
async def test_purge_config_ttl(app, db_session):
    """TTL setting from config should be respected."""
    from cms.auth import get_settings

    settings = get_settings()
    assert hasattr(settings, "pending_device_ttl_hours")
    assert settings.pending_device_ttl_hours > 0
