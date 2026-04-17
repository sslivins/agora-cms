"""Tests for the event/notification split in AlertService.

Events always fire immediately; notifications are gated by the grace period.
"""

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_event import DeviceEvent, DeviceEventType
from cms.models.notification import Notification
from cms.services.alert_service import AlertService


@pytest_asyncio.fixture
async def seed_group_and_device(app):
    """Adopted device in a group, isolated from other alert tests."""
    from cms.database import get_db
    factory = app.dependency_overrides[get_db]
    async for db in factory():
        group = DeviceGroup(name="Split Test Group")
        db.add(group)
        await db.flush()
        device = Device(
            id="split-device-001",
            name="Split Device",
            status=DeviceStatus.ADOPTED,
            group_id=group.id,
        )
        db.add(device)
        await db.commit()
        yield {
            "device_id": device.id,
            "device_name": device.name,
            "group_id": str(group.id),
            "group_name": group.name,
        }
        break


@pytest_asyncio.fixture
def fresh_alert_service():
    svc = AlertService()
    svc._offline_grace_seconds = 1
    svc._temp_cooldown_seconds = 1
    return svc


async def _count_events(app, device_id, event_type=None):
    from cms.database import get_db
    factory = app.dependency_overrides[get_db]
    async for db in factory():
        q = select(DeviceEvent).where(DeviceEvent.device_id == device_id)
        if event_type is not None:
            q = q.where(DeviceEvent.event_type == event_type)
        events = (await db.execute(q)).scalars().all()
        return events


async def _count_notifications(app, group_id):
    from cms.database import get_db
    factory = app.dependency_overrides[get_db]
    async for db in factory():
        result = await db.execute(
            select(Notification).where(Notification.group_id == uuid.UUID(group_id))
        )
        return result.scalars().all()


# ── Event fires immediately ──


@pytest.mark.asyncio
async def test_disconnect_creates_offline_event_before_grace(
    app, seed_group_and_device, fresh_alert_service,
):
    """An OFFLINE DeviceEvent is created immediately on disconnect — no wait."""
    info = seed_group_and_device
    svc = fresh_alert_service
    svc._offline_grace_seconds = 30  # Long grace — ensure event fires before

    svc.device_disconnected(info["device_id"], info["device_name"],
                             info["group_id"], info["group_name"], status="adopted")
    # Let the immediate create_task run
    await asyncio.sleep(0.2)

    events = await _count_events(app, info["device_id"], DeviceEventType.OFFLINE)
    assert len(events) == 1

    # No notification yet (grace hasn't fired)
    notifs = await _count_notifications(app, info["group_id"])
    assert len(notifs) == 0


# ── Reconnect before grace: ONLINE event, no notifications ──


@pytest.mark.asyncio
async def test_reconnect_before_grace_no_notification_but_events_logged(
    app, seed_group_and_device, fresh_alert_service,
):
    info = seed_group_and_device
    svc = fresh_alert_service
    svc._offline_grace_seconds = 30  # Make sure grace never expires during test

    svc.device_disconnected(info["device_id"], info["device_name"],
                             info["group_id"], info["group_name"], status="adopted")
    await asyncio.sleep(0.1)
    svc.device_reconnected(info["device_id"], info["device_name"],
                            info["group_id"], info["group_name"], status="adopted")
    await asyncio.sleep(0.3)

    # Both OFFLINE and ONLINE events were logged
    offline_events = await _count_events(app, info["device_id"], DeviceEventType.OFFLINE)
    online_events = await _count_events(app, info["device_id"], DeviceEventType.ONLINE)
    assert len(offline_events) == 1
    assert len(online_events) == 1

    # But NO notifications (neither offline nor back-online)
    notifs = await _count_notifications(app, info["group_id"])
    assert len(notifs) == 0

    # Grace timer was cancelled
    assert info["device_id"] not in svc._offline_timers or \
           svc._offline_timers[info["device_id"]].task.cancelled()


# ── Offline past grace, then reconnect: offline + online notifications ──


@pytest.mark.asyncio
async def test_offline_past_grace_then_reconnect_fires_both_notifications(
    app, seed_group_and_device, fresh_alert_service,
):
    info = seed_group_and_device
    svc = fresh_alert_service
    svc._offline_grace_seconds = 1

    # Patch device_manager.is_connected to ensure grace expiration fires
    from cms.services import device_manager as _dm_mod
    original = _dm_mod.device_manager.is_connected
    _dm_mod.device_manager.is_connected = lambda _did: False
    try:
        svc.device_disconnected(info["device_id"], info["device_name"],
                                 info["group_id"], info["group_name"], status="adopted")
        # Wait past grace
        await asyncio.sleep(1.5)

        # Offline notification should exist
        notifs = await _count_notifications(app, info["group_id"])
        offline_notifs = [n for n in notifs if "offline" in n.title.lower()]
        assert len(offline_notifs) == 1

        # Now reconnect — should fire ONLINE event + "back online" notification
        svc.device_reconnected(info["device_id"], info["device_name"],
                                info["group_id"], info["group_name"], status="adopted")
        await asyncio.sleep(0.3)

        online_events = await _count_events(app, info["device_id"], DeviceEventType.ONLINE)
        assert len(online_events) == 1

        notifs = await _count_notifications(app, info["group_id"])
        back_online = [n for n in notifs if "back online" in n.title.lower()]
        assert len(back_online) == 1
    finally:
        _dm_mod.device_manager.is_connected = original


# ── Disconnect → quick reconnect → disconnect again past grace ──


@pytest.mark.asyncio
async def test_fresh_grace_timer_after_quick_reconnect(
    app, seed_group_and_device, fresh_alert_service,
):
    """A prior cancelled grace shouldn't leak — second disconnect gets a fresh timer."""
    info = seed_group_and_device
    svc = fresh_alert_service
    svc._offline_grace_seconds = 1

    from cms.services import device_manager as _dm_mod
    original = _dm_mod.device_manager.is_connected
    _dm_mod.device_manager.is_connected = lambda _did: False
    try:
        # Disconnect — starts timer
        svc.device_disconnected(info["device_id"], info["device_name"],
                                 info["group_id"], info["group_name"], status="adopted")
        await asyncio.sleep(0.1)
        # Reconnect quickly — timer cancelled, no notifications yet
        svc.device_reconnected(info["device_id"], info["device_name"],
                                info["group_id"], info["group_name"], status="adopted")
        await asyncio.sleep(0.2)
        notifs = await _count_notifications(app, info["group_id"])
        assert len(notifs) == 0

        # Now disconnect again and let grace expire
        svc.device_disconnected(info["device_id"], info["device_name"],
                                 info["group_id"], info["group_name"], status="adopted")
        await asyncio.sleep(1.5)

        # Offline notification should now exist
        notifs = await _count_notifications(app, info["group_id"])
        offline_notifs = [n for n in notifs if "offline" in n.title.lower()]
        assert len(offline_notifs) == 1

        # Two OFFLINE events (one per disconnect)
        offline_events = await _count_events(app, info["device_id"], DeviceEventType.OFFLINE)
        assert len(offline_events) == 2
    finally:
        _dm_mod.device_manager.is_connected = original
