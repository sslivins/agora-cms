"""Tests for the display-disconnected alert path in alert_service."""

import asyncio
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select

from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.notification import Notification
from cms.services.alert_service import AlertService


@pytest_asyncio.fixture
async def seed_group_and_device(app):
    """Create an adopted device in a group for display alert testing."""
    from cms.database import get_db
    factory = app.dependency_overrides[get_db]

    async for db in factory():
        group = DeviceGroup(name="Display Alert Group")
        db.add(group)
        await db.flush()

        device = Device(
            id="display-device-001",
            name="Test Display Device",
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
    """Fresh AlertService with short grace periods for fast tests."""
    svc = AlertService()
    svc._offline_grace_seconds = 1
    svc._display_grace_seconds = 1
    svc._temp_cooldown_seconds = 1
    return svc


class TestDisplayDisconnect:
    """Display-disconnected grace period and notifications."""

    @pytest.mark.asyncio
    async def test_grace_period_fires_notification(self, app, seed_group_and_device, fresh_alert_service):
        """After the display grace period expires, a warning notification is created.

        Requires the device to still be marked display_disconnected in
        device_manager when the grace timer fires.
        """
        info = seed_group_and_device
        svc = fresh_alert_service

        # Mark the device as connected via device_manager so the grace
        # callback's "still disconnected?" check finds it. We set
        # display_connected=False to indicate the display is off.
        from cms.services.device_manager import device_manager
        device_manager.register(info["device_id"], websocket=None)
        device_manager.update_status(
            info["device_id"], mode="idle", asset=None, display_connected=False,
        )

        try:
            svc.display_state_changed(
                info["device_id"], info["device_name"],
                info["group_id"], info["group_name"],
                status="adopted", connected=False,
            )

            await asyncio.sleep(1.5)

            from cms.database import get_db
            factory = app.dependency_overrides[get_db]
            async for db in factory():
                notifs = (await db.execute(
                    select(Notification).where(
                        Notification.group_id == uuid.UUID(info["group_id"]),
                        Notification.level == "warning",
                    )
                )).scalars().all()
                assert any("display disconnected" in n.title.lower() for n in notifs), \
                    [n.title for n in notifs]
                break
        finally:
            device_manager.disconnect(info["device_id"])

    @pytest.mark.asyncio
    async def test_reconnect_cancels_grace_period(self, app, seed_group_and_device, fresh_alert_service):
        """A quick display-reconnect before grace expires suppresses the notification."""
        info = seed_group_and_device
        svc = fresh_alert_service

        from cms.services.device_manager import device_manager
        device_manager.register(info["device_id"], websocket=None)
        device_manager.update_status(
            info["device_id"], mode="idle", asset=None, display_connected=False,
        )

        try:
            svc.display_state_changed(
                info["device_id"], info["device_name"],
                info["group_id"], info["group_name"],
                status="adopted", connected=False,
            )
            # Reconnect immediately
            svc.display_state_changed(
                info["device_id"], info["device_name"],
                info["group_id"], info["group_name"],
                status="adopted", connected=True,
            )

            await asyncio.sleep(1.5)

            from cms.database import get_db
            factory = app.dependency_overrides[get_db]
            async for db in factory():
                notifs = (await db.execute(
                    select(Notification).where(
                        Notification.group_id == uuid.UUID(info["group_id"]),
                    )
                )).scalars().all()
                assert not any("display disconnected" in n.title.lower() for n in notifs)
                # Also: no "display reconnected" notification, since the warning
                # never fired in the first place.
                assert not any("display reconnected" in n.title.lower() for n in notifs)
                break
        finally:
            device_manager.disconnect(info["device_id"])

    @pytest.mark.asyncio
    async def test_reconnect_after_warning_fires_recovery(self, app, seed_group_and_device, fresh_alert_service):
        """A reconnect after the warning was sent fires a "display reconnected" info."""
        info = seed_group_and_device
        svc = fresh_alert_service

        from cms.services.device_manager import device_manager
        device_manager.register(info["device_id"], websocket=None)
        device_manager.update_status(
            info["device_id"], mode="idle", asset=None, display_connected=False,
        )

        try:
            svc.display_state_changed(
                info["device_id"], info["device_name"],
                info["group_id"], info["group_name"],
                status="adopted", connected=False,
            )

            # Wait long enough for the warning to fire
            await asyncio.sleep(1.5)

            # Now display comes back
            svc.display_state_changed(
                info["device_id"], info["device_name"],
                info["group_id"], info["group_name"],
                status="adopted", connected=True,
            )

            # Give the recovery notification a moment to commit
            await asyncio.sleep(0.3)

            from cms.database import get_db
            factory = app.dependency_overrides[get_db]
            async for db in factory():
                notifs = (await db.execute(
                    select(Notification).where(
                        Notification.group_id == uuid.UUID(info["group_id"]),
                    )
                )).scalars().all()
                titles = [n.title.lower() for n in notifs]
                assert any("display disconnected" in t for t in titles), titles
                assert any("display reconnected" in t for t in titles), titles
                break
        finally:
            device_manager.disconnect(info["device_id"])

    @pytest.mark.asyncio
    async def test_pending_device_no_alert(self, fresh_alert_service):
        """Pending (unadopted) devices don't trigger display alerts."""
        svc = fresh_alert_service
        svc.display_state_changed(
            "pending-d", "Pending Display",
            group_id=str(uuid.uuid4()), group_name="G",
            status="pending", connected=False,
        )
        assert "pending-d" not in svc._display_timers

    @pytest.mark.asyncio
    async def test_ungrouped_device_no_alert(self, fresh_alert_service):
        """Adopted devices without a group don't trigger display alerts."""
        svc = fresh_alert_service
        svc.display_state_changed(
            "ungrouped-d", "Ungrouped Display",
            group_id=None, group_name="",
            status="adopted", connected=False,
        )
        assert "ungrouped-d" not in svc._display_timers

    @pytest.mark.asyncio
    async def test_device_offline_during_grace_suppresses_display_notif(
        self, app, seed_group_and_device, fresh_alert_service,
    ):
        """If the device goes fully offline during the display grace period,
        no display-disconnected notification fires (offline alert covers it)."""
        info = seed_group_and_device
        svc = fresh_alert_service

        from cms.services.device_manager import device_manager
        device_manager.register(info["device_id"], websocket=None)
        device_manager.update_status(
            info["device_id"], mode="idle", asset=None, display_connected=False,
        )

        try:
            svc.display_state_changed(
                info["device_id"], info["device_name"],
                info["group_id"], info["group_name"],
                status="adopted", connected=False,
            )

            # Simulate full WS disconnect mid-grace
            device_manager.disconnect(info["device_id"])
            svc.device_disconnected(
                info["device_id"], info["device_name"],
                info["group_id"], info["group_name"],
                status="adopted",
            )

            await asyncio.sleep(1.5)

            from cms.database import get_db
            factory = app.dependency_overrides[get_db]
            async for db in factory():
                notifs = (await db.execute(
                    select(Notification).where(
                        Notification.group_id == uuid.UUID(info["group_id"]),
                    )
                )).scalars().all()
                assert not any("display disconnected" in n.title.lower() for n in notifs)
                break
        finally:
            # device already disconnected; safe to call again
            device_manager.disconnect(info["device_id"])

    @pytest.mark.asyncio
    async def test_cleanup_cancels_display_timer(self, fresh_alert_service):
        """cleanup_device cancels any pending display grace timer."""
        svc = fresh_alert_service
        gid = str(uuid.uuid4())
        svc.display_state_changed(
            "dev-d", "Display", gid, "G",
            status="adopted", connected=False,
        )
        assert "dev-d" in svc._display_timers
        svc._was_display_off.add("dev-d")

        svc.cleanup_device("dev-d")
        assert "dev-d" not in svc._display_timers
        assert "dev-d" not in svc._was_display_off
