"""Tests for the device alert system — offline detection and temperature alerts."""

import asyncio
import uuid

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_event import DeviceEvent, DeviceEventType
from cms.models.notification import Notification
from cms.models.notification_pref import UserNotificationPref
from cms.models.user import User, UserGroup
from cms.services.alert_service import AlertService


# ── Fixtures ──

@pytest_asyncio.fixture
async def seed_group_and_device(app):
    """Create an adopted device in a group for alert testing."""
    from cms.database import get_db
    factory = app.dependency_overrides[get_db]

    async for db in factory():
        group = DeviceGroup(name="Alert Test Group")
        db.add(group)
        await db.flush()

        device = Device(
            id="alert-device-001",
            name="Test Display",
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
async def operator_client(app, seed_group_and_device):
    """Authenticated client for an Operator user in the test group."""
    from cms.auth import hash_password
    from cms.database import get_db

    factory = app.dependency_overrides[get_db]
    async for db in factory():
        result = await db.execute(
            select(DeviceGroup).where(DeviceGroup.name == "Alert Test Group")
        )
        group = result.scalar_one()

        from cms.models.user import Role
        result = await db.execute(select(Role).where(Role.name == "Operator"))
        role = result.scalar_one()

        user = User(
            username="alert-operator",
            email="alert-operator@test.com",
            display_name="Alert Operator",
            password_hash=hash_password("oppass"),
            role_id=role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(user)
        await db.flush()

        if group:
            ug = UserGroup(user_id=user.id, group_id=group.id)
            db.add(ug)
        await db.commit()
        break

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        await ac.post("/login", data={"username": "alert-operator", "password": "oppass"},
                       follow_redirects=False)
        yield ac


@pytest_asyncio.fixture
def fresh_alert_service():
    """Return a fresh AlertService instance with short grace period for testing."""
    svc = AlertService()
    svc._offline_grace_seconds = 1  # 1 second for fast tests
    svc._temp_cooldown_seconds = 1
    return svc


# ── Alert Service Unit Tests ──

class TestOfflineDetection:
    """Test offline grace period and notification creation."""

    @pytest.mark.asyncio
    async def test_grace_period_fires_offline_event(self, app, seed_group_and_device, fresh_alert_service):
        """After grace period expires, an OFFLINE DeviceEvent and Notification are created."""
        info = seed_group_and_device
        svc = fresh_alert_service

        svc.device_disconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )

        # Wait for grace period to expire
        await asyncio.sleep(1.5)

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == info["device_id"],
                    DeviceEvent.event_type == DeviceEventType.OFFLINE,
                )
            )).scalars().all()
            assert len(events) == 1
            assert events[0].device_name == info["device_name"]
            assert events[0].group_name == info["group_name"]

            notifs = (await db.execute(
                select(Notification).where(
                    Notification.group_id == uuid.UUID(info["group_id"]),
                    Notification.level == "warning",
                )
            )).scalars().all()
            assert len(notifs) >= 1
            assert "offline" in notifs[0].title.lower()
            break

    @pytest.mark.asyncio
    async def test_reconnect_cancels_grace_period(self, app, seed_group_and_device, fresh_alert_service):
        """Reconnecting before grace period expires prevents the offline *notification*.

        Note: since the event/notification split, the OFFLINE DeviceEvent is
        logged immediately on disconnect regardless of grace, but the bell
        notification is only created after grace expires. A quick reconnect
        cancels the pending notification timer.
        """
        info = seed_group_and_device
        svc = fresh_alert_service

        svc.device_disconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )

        # Reconnect immediately
        svc.device_reconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )

        await asyncio.sleep(1.5)

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            # No offline *notification* should have been created
            notifs = (await db.execute(
                select(Notification).where(
                    Notification.group_id == uuid.UUID(info["group_id"]),
                )
            )).scalars().all()
            assert not any("offline" in n.title.lower() for n in notifs)
            break

    @pytest.mark.asyncio
    async def test_back_online_after_offline(self, app, seed_group_and_device, fresh_alert_service):
        """Reconnecting after an offline event fires creates an ONLINE event."""
        info = seed_group_and_device
        svc = fresh_alert_service

        svc.device_disconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )

        # Wait for offline to fire
        await asyncio.sleep(1.5)

        # Now reconnect
        svc.device_reconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )

        await asyncio.sleep(0.5)

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == info["device_id"],
                ).order_by(DeviceEvent.created_at)
            )).scalars().all()
            types = [e.event_type for e in events]
            assert DeviceEventType.OFFLINE in types
            assert DeviceEventType.ONLINE in types

            # Online notification should exist
            notifs = (await db.execute(
                select(Notification).where(
                    Notification.group_id == uuid.UUID(info["group_id"]),
                    Notification.level == "info",
                )
            )).scalars().all()
            assert any("back online" in n.title.lower() for n in notifs)
            break

    @pytest.mark.asyncio
    async def test_pending_device_no_alert(self, fresh_alert_service):
        """Pending (unadopted) devices don't trigger alerts."""
        svc = fresh_alert_service
        svc.device_disconnected(
            "pending-001", "Pending Device",
            group_id=str(uuid.uuid4()), group_name="SomeGroup",
            status="pending",
        )
        # Should have no timer
        assert "pending-001" not in svc._offline_timers

    @pytest.mark.asyncio
    async def test_ungrouped_device_no_alert(self, fresh_alert_service):
        """Adopted devices without a group don't trigger alerts."""
        svc = fresh_alert_service
        svc.device_disconnected(
            "ungrouped-001", "Ungrouped Device",
            group_id=None, group_name="",
            status="adopted",
        )
        assert "ungrouped-001" not in svc._offline_timers


class TestTemperatureAlerts:
    """Test temperature threshold monitoring and hysteresis."""

    @pytest.mark.asyncio
    async def test_warning_threshold(self, app, seed_group_and_device, fresh_alert_service):
        """Crossing the warning threshold creates a TEMP_HIGH event."""
        info = seed_group_and_device
        svc = fresh_alert_service

        # Send temp below threshold — no alert
        svc.check_temperature(
            info["device_id"], 65.0, info["device_name"],
            info["group_id"], info["group_name"], status="adopted",
        )
        await asyncio.sleep(0.3)

        # Send temp above warning
        svc.check_temperature(
            info["device_id"], 72.0, info["device_name"],
            info["group_id"], info["group_name"], status="adopted",
        )
        await asyncio.sleep(0.5)

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == info["device_id"],
                    DeviceEvent.event_type == DeviceEventType.TEMP_HIGH,
                )
            )).scalars().all()
            assert len(events) == 1
            assert events[0].details["level"] == "warning"

            notifs = (await db.execute(
                select(Notification).where(
                    Notification.group_id == uuid.UUID(info["group_id"]),
                    Notification.level == "warning",
                )
            )).scalars().all()
            assert any("temperature" in n.title.lower() or "high" in n.title.lower() for n in notifs)
            break

    @pytest.mark.asyncio
    async def test_critical_threshold(self, app, seed_group_and_device, fresh_alert_service):
        """Crossing the critical threshold creates an error-level notification."""
        info = seed_group_and_device
        svc = fresh_alert_service

        svc.check_temperature(
            info["device_id"], 85.0, info["device_name"],
            info["group_id"], info["group_name"], status="adopted",
        )
        await asyncio.sleep(0.5)

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == info["device_id"],
                    DeviceEvent.event_type == DeviceEventType.TEMP_HIGH,
                )
            )).scalars().all()
            assert len(events) == 1
            assert events[0].details["level"] == "critical"

            notifs = (await db.execute(
                select(Notification).where(
                    Notification.group_id == uuid.UUID(info["group_id"]),
                    Notification.level == "error",
                )
            )).scalars().all()
            assert len(notifs) >= 1
            break

    @pytest.mark.asyncio
    async def test_temp_cleared(self, app, seed_group_and_device, fresh_alert_service):
        """Temperature returning to normal creates a TEMP_CLEARED event."""
        info = seed_group_and_device
        svc = fresh_alert_service

        # Go high
        svc.check_temperature(
            info["device_id"], 75.0, info["device_name"],
            info["group_id"], info["group_name"], status="adopted",
        )
        await asyncio.sleep(0.5)

        # Go back to normal
        svc.check_temperature(
            info["device_id"], 55.0, info["device_name"],
            info["group_id"], info["group_name"], status="adopted",
        )
        await asyncio.sleep(0.5)

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == info["device_id"],
                ).order_by(DeviceEvent.created_at)
            )).scalars().all()
            types = [e.event_type for e in events]
            assert DeviceEventType.TEMP_HIGH in types
            assert DeviceEventType.TEMP_CLEARED in types

            notifs = (await db.execute(
                select(Notification).where(
                    Notification.group_id == uuid.UUID(info["group_id"]),
                    Notification.level == "info",
                )
            )).scalars().all()
            assert any("normal" in n.title.lower() for n in notifs)
            break

    @pytest.mark.asyncio
    async def test_no_duplicate_alerts(self, fresh_alert_service):
        """Repeated heartbeats at same temp level don't create duplicate alerts."""
        svc = fresh_alert_service
        gid = str(uuid.uuid4())

        # First: go warning
        svc.check_temperature("dev1", 75.0, "Dev1", gid, "G", status="adopted")
        # Same level again — should be ignored
        svc.check_temperature("dev1", 76.0, "Dev1", gid, "G", status="adopted")
        svc.check_temperature("dev1", 74.0, "Dev1", gid, "G", status="adopted")

        # Only one transition happened
        state = svc._temp_states["dev1"]
        assert state.level == "warning"

    @pytest.mark.asyncio
    async def test_cooldown_prevents_flapping(self, fresh_alert_service):
        """After temp clears, cooldown prevents immediate re-alert."""
        svc = fresh_alert_service
        svc._temp_cooldown_seconds = 60  # Long cooldown
        gid = str(uuid.uuid4())

        # Go high
        svc.check_temperature("dev2", 75.0, "Dev2", gid, "G", status="adopted")
        # Go normal (cleared)
        svc.check_temperature("dev2", 55.0, "Dev2", gid, "G", status="adopted")
        # Go high again within cooldown — should be suppressed
        svc.check_temperature("dev2", 75.0, "Dev2", gid, "G", status="adopted")

        state = svc._temp_states["dev2"]
        assert state.level == "normal"  # Didn't transition because of cooldown

    @pytest.mark.asyncio
    async def test_none_temp_ignored(self, fresh_alert_service):
        """None temperature values are silently ignored."""
        svc = fresh_alert_service
        svc.check_temperature("dev3", None, "Dev3", str(uuid.uuid4()), "G", status="adopted")
        assert "dev3" not in svc._temp_states

    @pytest.mark.asyncio
    async def test_pending_device_temp_ignored(self, fresh_alert_service):
        """Pending devices don't trigger temperature alerts."""
        svc = fresh_alert_service
        svc.check_temperature("dev4", 85.0, "Dev4", str(uuid.uuid4()), "G", status="pending")
        assert "dev4" not in svc._temp_states


# ── Device Events API Tests ──

class TestDeviceEventsAPI:
    """Test the /api/device-events endpoints."""

    @pytest.mark.asyncio
    async def test_list_events_empty(self, client):
        resp = await client.get("/api/device-events")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_list_events_after_offline(self, client, app, seed_group_and_device, fresh_alert_service):
        info = seed_group_and_device
        svc = fresh_alert_service
        svc.device_disconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )
        await asyncio.sleep(1.5)

        resp = await client.get("/api/device-events")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) >= 1
        assert events[0]["event_type"] == "offline"
        assert events[0]["device_id"] == info["device_id"]

    @pytest.mark.asyncio
    async def test_filter_by_device_id(self, client, app, seed_group_and_device, fresh_alert_service):
        info = seed_group_and_device
        svc = fresh_alert_service
        svc.device_disconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )
        await asyncio.sleep(1.5)

        resp = await client.get(f"/api/device-events?device_id={info['device_id']}")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

        resp = await client.get("/api/device-events?device_id=nonexistent")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_filter_by_event_type(self, client, app, seed_group_and_device, fresh_alert_service):
        info = seed_group_and_device
        svc = fresh_alert_service
        svc.device_disconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )
        await asyncio.sleep(1.5)

        resp = await client.get("/api/device-events?event_type=offline")
        assert resp.status_code == 200
        assert len(resp.json()) >= 1

        resp = await client.get("/api/device-events?event_type=online")
        assert resp.status_code == 200
        assert resp.json() == []

    @pytest.mark.asyncio
    async def test_event_count(self, client, app, seed_group_and_device, fresh_alert_service):
        info = seed_group_and_device
        svc = fresh_alert_service
        svc.device_disconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )
        await asyncio.sleep(1.5)

        resp = await client.get("/api/device-events/count")
        assert resp.status_code == 200
        assert resp.json()["count"] >= 1

    @pytest.mark.asyncio
    async def test_operator_sees_own_group_events(self, operator_client, app, seed_group_and_device, fresh_alert_service):
        """Operator sees events for devices in their group."""
        info = seed_group_and_device
        svc = fresh_alert_service
        svc.device_disconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )
        await asyncio.sleep(1.5)

        resp = await operator_client.get("/api/device-events")
        assert resp.status_code == 200
        events = resp.json()
        assert len(events) >= 1


# ── Notification Preferences API Tests ──

class TestNotificationPrefsAPI:
    """Test the /api/notification-preferences endpoints."""

    @pytest.mark.asyncio
    async def test_get_creates_defaults(self, client):
        resp = await client.get("/api/notification-preferences")
        assert resp.status_code == 200
        prefs = resp.json()
        assert len(prefs) == 6
        types = {p["event_type"] for p in prefs}
        assert types == {"offline", "online", "temp_high", "temp_cleared",
                         "display_disconnected", "display_connected"}
        assert all(p["email_enabled"] is False for p in prefs)

    @pytest.mark.asyncio
    async def test_update_preferences(self, client):
        # First get to create defaults
        await client.get("/api/notification-preferences")

        # Update offline to email_enabled=True
        resp = await client.put(
            "/api/notification-preferences",
            json=[{"event_type": "offline", "email_enabled": True}],
        )
        assert resp.status_code == 200
        prefs = resp.json()
        offline_pref = next(p for p in prefs if p["event_type"] == "offline")
        assert offline_pref["email_enabled"] is True

        # Others unchanged
        online_pref = next(p for p in prefs if p["event_type"] == "online")
        assert online_pref["email_enabled"] is False

    @pytest.mark.asyncio
    async def test_email_status_disabled_by_default(self, client):
        resp = await client.get("/api/notification-preferences/email-status")
        assert resp.status_code == 200
        assert resp.json()["enabled"] is False

    @pytest.mark.asyncio
    async def test_invalid_event_type_ignored(self, client):
        await client.get("/api/notification-preferences")
        resp = await client.put(
            "/api/notification-preferences",
            json=[{"event_type": "bogus_event", "email_enabled": True}],
        )
        assert resp.status_code == 200


# ── Cleanup test ──

class TestAlertServiceCleanup:
    """Test cleanup of in-memory state."""

    @pytest.mark.asyncio
    async def test_cleanup_cancels_timer(self, fresh_alert_service):
        svc = fresh_alert_service
        gid = str(uuid.uuid4())
        svc.device_disconnected("dev-clean", "Cleanup Dev", gid, "G", status="adopted")
        assert "dev-clean" in svc._offline_timers

        svc.cleanup_device("dev-clean")
        assert "dev-clean" not in svc._offline_timers
        assert "dev-clean" not in svc._temp_states

    @pytest.mark.asyncio
    async def test_cleanup_removes_temp_state(self, fresh_alert_service):
        svc = fresh_alert_service
        svc.check_temperature("dev-tc", 75.0, "D", str(uuid.uuid4()), "G", status="adopted")
        assert "dev-tc" in svc._temp_states

        svc.cleanup_device("dev-tc")
        assert "dev-tc" not in svc._temp_states
