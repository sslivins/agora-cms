"""Tests for the device alert system — offline detection and temperature alerts."""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_alert_state import DeviceAlertState
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

        # Let the detached _record_disconnect task run and write
        # offline_since, then let the grace window elapse.
        await asyncio.sleep(0.1)
        await asyncio.sleep(1.5)

        # The sweep is normally driven from a leader-gated loop in
        # cms.main; under unit tests we invoke it directly.
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            emitted = await svc.offline_sweep_once(db)
            assert emitted == 1
            break

        async for db in factory():
            events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == info["device_id"],
                    DeviceEvent.event_type == DeviceEventType.OFFLINE,
                )
            )).scalars().all()
            assert len(events) >= 1
            assert events[0].device_name == info["device_name"]
            assert events[0].group_name == info["group_name"]

            notifs = (await db.execute(
                select(Notification).where(
                    Notification.group_id == uuid.UUID(info["group_id"]),
                    Notification.level == "error",
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
        """Reconnecting after an offline event fires creates an ONLINE event and back-online notification."""
        info = seed_group_and_device
        svc = fresh_alert_service

        svc.device_disconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )

        # Let _record_disconnect land, then wait past grace and run the
        # sweep so offline_notified=True.
        await asyncio.sleep(0.1)
        await asyncio.sleep(1.5)

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await svc.offline_sweep_once(db)
            break

        # Now reconnect — CAS-consume path should emit back-online.
        svc.device_reconnected(
            info["device_id"], info["device_name"],
            info["group_id"], info["group_name"],
            status="adopted",
        )
        await asyncio.sleep(0.3)

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
                    Notification.level == "success",
                )
            )).scalars().all()
            assert any("back online" in n.title.lower() for n in notifs)
            break

    @pytest.mark.asyncio
    async def test_pending_device_no_alert(self, app, fresh_alert_service):
        """Pending (unadopted) devices don't record alert state."""
        svc = fresh_alert_service
        svc.device_disconnected(
            "pending-001", "Pending Device",
            group_id=str(uuid.uuid4()), group_name="SomeGroup",
            status="pending",
        )
        await asyncio.sleep(0.2)
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            state = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == "pending-001"
                )
            )).scalar_one_or_none()
            assert state is None
            break

    @pytest.mark.asyncio
    async def test_ungrouped_device_no_alert(self, app, fresh_alert_service):
        """Adopted devices without a group don't record alert state."""
        svc = fresh_alert_service
        svc.device_disconnected(
            "ungrouped-001", "Ungrouped Device",
            group_id=None, group_name="",
            status="adopted",
        )
        await asyncio.sleep(0.2)
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            state = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == "ungrouped-001"
                )
            )).scalar_one_or_none()
            assert state is None
            break


class TestTemperatureAlerts:
    """Test temperature threshold monitoring and hysteresis (DB-backed)."""

    _UNSET = object()

    async def _call(
        self, app, svc, info, temp, *, sample_ts=None, status="adopted",
        group_id=_UNSET,
    ):
        """Helper: run ``check_temperature`` against the test DB."""
        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            await svc.check_temperature(
                db,
                info["device_id"],
                cpu_temp_c=temp,
                device_name=info["device_name"],
                group_id=info["group_id"] if group_id is self._UNSET else group_id,
                group_name=info["group_name"],
                status=status,
                sample_ts=sample_ts,
            )
            break

    @pytest.mark.asyncio
    async def test_warning_threshold(self, app, seed_group_and_device, fresh_alert_service):
        """Crossing the warning threshold creates a TEMP_HIGH event."""
        info = seed_group_and_device
        svc = fresh_alert_service
        t0 = datetime.now(timezone.utc)

        # Send temp below threshold — no alert
        await self._call(app, svc, info, 65.0, sample_ts=t0)
        # Send temp above warning
        await self._call(app, svc, info, 72.0, sample_ts=t0 + timedelta(seconds=1))

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

        await self._call(app, svc, info, 85.0, sample_ts=datetime.now(timezone.utc))

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
        t0 = datetime.now(timezone.utc)

        await self._call(app, svc, info, 75.0, sample_ts=t0)
        await self._call(app, svc, info, 55.0, sample_ts=t0 + timedelta(seconds=1))

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

            cleared = [e for e in events if e.event_type == DeviceEventType.TEMP_CLEARED]
            # previous_level should be preserved in details so operators
            # can see what was cleared from.
            assert cleared[0].details.get("previous_level") == "warning"

            notifs = (await db.execute(
                select(Notification).where(
                    Notification.group_id == uuid.UUID(info["group_id"]),
                    Notification.level == "success",
                )
            )).scalars().all()
            assert any("normal" in n.title.lower() for n in notifs)
            break

    @pytest.mark.asyncio
    async def test_no_duplicate_alerts_same_level(
        self, app, seed_group_and_device, fresh_alert_service,
    ):
        """Repeated heartbeats at same temp level, within cooldown, don't fire duplicates."""
        info = seed_group_and_device
        svc = fresh_alert_service
        svc._temp_cooldown_seconds = 60
        t0 = datetime.now(timezone.utc)

        await self._call(app, svc, info, 75.0, sample_ts=t0)
        # Subsequent same-level samples within cooldown: no new event.
        await self._call(app, svc, info, 76.0, sample_ts=t0 + timedelta(seconds=1))
        await self._call(app, svc, info, 74.0, sample_ts=t0 + timedelta(seconds=2))

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
            break

    @pytest.mark.asyncio
    async def test_reminder_fires_after_cooldown(
        self, app, seed_group_and_device, fresh_alert_service,
    ):
        """Sustained high-temp past cooldown fires a reminder TEMP_HIGH.

        User requirement: "never miss a high-temp alert."
        """
        info = seed_group_and_device
        svc = fresh_alert_service
        svc._temp_cooldown_seconds = 10
        t0 = datetime.now(timezone.utc)

        await self._call(app, svc, info, 75.0, sample_ts=t0)
        # Past cooldown, still warning — should fire a reminder.
        await self._call(app, svc, info, 75.0, sample_ts=t0 + timedelta(seconds=15))

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == info["device_id"],
                    DeviceEvent.event_type == DeviceEventType.TEMP_HIGH,
                )
            )).scalars().all()
            assert len(events) == 2
            break

    @pytest.mark.asyncio
    async def test_escalation_bypasses_cooldown(
        self, app, seed_group_and_device, fresh_alert_service,
    ):
        """warning→critical always fires, even inside cooldown window."""
        info = seed_group_and_device
        svc = fresh_alert_service
        svc._temp_cooldown_seconds = 600  # Long cooldown
        t0 = datetime.now(timezone.utc)

        await self._call(app, svc, info, 75.0, sample_ts=t0)
        # Within cooldown, escalating to critical: MUST fire.
        await self._call(app, svc, info, 85.0, sample_ts=t0 + timedelta(seconds=1))

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            events = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == info["device_id"],
                    DeviceEvent.event_type == DeviceEventType.TEMP_HIGH,
                )
            )).scalars().all()
            assert len(events) == 2
            levels = [e.details["level"] for e in events]
            assert "warning" in levels
            assert "critical" in levels
            break

    @pytest.mark.asyncio
    async def test_stale_sample_ignored(
        self, app, seed_group_and_device, fresh_alert_service,
    ):
        """Out-of-order delivery: older sample_ts doesn't overwrite newer state."""
        info = seed_group_and_device
        svc = fresh_alert_service
        t0 = datetime.now(timezone.utc)

        await self._call(app, svc, info, 75.0, sample_ts=t0)
        # Stale sample — older ts, lower temp. Must be ignored.
        await self._call(app, svc, info, 55.0, sample_ts=t0 - timedelta(seconds=30))

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            state = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == info["device_id"]
                )
            )).scalar_one()
            assert state.temp_level == "warning"
            # SQLite strips tzinfo on round-trip; compare naive-UTC.
            stored_ts = state.temp_last_sample_ts
            if stored_ts.tzinfo is not None:
                stored_ts = stored_ts.astimezone(timezone.utc).replace(tzinfo=None)
            assert stored_ts == t0.replace(tzinfo=None)

            cleared = (await db.execute(
                select(DeviceEvent).where(
                    DeviceEvent.device_id == info["device_id"],
                    DeviceEvent.event_type == DeviceEventType.TEMP_CLEARED,
                )
            )).scalars().all()
            assert len(cleared) == 0
            break

    @pytest.mark.asyncio
    async def test_none_temp_ignored(self, app, seed_group_and_device, fresh_alert_service):
        """None temperature values are silently ignored (no state written)."""
        info = seed_group_and_device
        svc = fresh_alert_service
        await self._call(app, svc, info, None, sample_ts=datetime.now(timezone.utc))

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            state = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == info["device_id"]
                )
            )).scalar_one_or_none()
            # Either no row or default 'normal' state — just assert no
            # non-normal state leaked.
            assert state is None or state.temp_level == "normal"
            break

    @pytest.mark.asyncio
    async def test_pending_device_temp_resets_state(
        self, app, seed_group_and_device, fresh_alert_service,
    ):
        """Non-adopted heartbeat resets any persisted temp state.

        Prevents the following bug: device A warns at 75°C, gets
        unadopted, later re-adopted in a different group — first
        warning should fire cleanly, not be suppressed by stale state.
        """
        info = seed_group_and_device
        svc = fresh_alert_service
        t0 = datetime.now(timezone.utc)

        await self._call(app, svc, info, 75.0, sample_ts=t0)
        # Now pretend the device got unadopted and sends a pending
        # heartbeat — state should reset.
        await self._call(
            app, svc, info, 90.0, sample_ts=t0 + timedelta(seconds=1),
            status="pending",
        )

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            state = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == info["device_id"]
                )
            )).scalar_one_or_none()
            assert state is not None
            assert state.temp_level == "normal"
            assert state.temp_last_alert_at is None
            assert state.temp_last_sample_ts is None
            break

    @pytest.mark.asyncio
    async def test_ungrouped_device_temp_resets_state(
        self, app, seed_group_and_device, fresh_alert_service,
    ):
        """Ungrouped device resets temp state even if still adopted."""
        info = seed_group_and_device
        svc = fresh_alert_service
        t0 = datetime.now(timezone.utc)

        await self._call(app, svc, info, 75.0, sample_ts=t0)
        # Device got un-grouped — state should reset.
        await self._call(
            app, svc, info, 90.0, sample_ts=t0 + timedelta(seconds=1),
            group_id=None,
        )

        from cms.database import get_db
        factory = app.dependency_overrides[get_db]
        async for db in factory():
            state = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == info["device_id"]
                )
            )).scalar_one_or_none()
            assert state is not None
            assert state.temp_level == "normal"
            break

    @pytest.mark.asyncio
    async def test_sample_ts_fallback_to_now_when_missing(
        self, app, seed_group_and_device, fresh_alert_service,
    ):
        """Missing sample_ts falls back to server now() with a log warning."""
        info = seed_group_and_device
        svc = fresh_alert_service
        # No sample_ts — exercises the fallback path.
        await self._call(app, svc, info, 75.0, sample_ts=None)

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
            break


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
        assert len(prefs) == 4
        types = {p["event_type"] for p in prefs}
        assert types == {"offline", "online", "temp_high", "temp_cleared"}
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
    """Test cleanup_device is a safe no-op post-DB-migration."""

    @pytest.mark.asyncio
    async def test_cleanup_is_noop(self, fresh_alert_service):
        """cleanup_device is a no-op (temp state lives in DB, cascade-deleted)."""
        svc = fresh_alert_service
        # Should not raise — previously cleared in-memory state, now
        # delegated to the ON DELETE CASCADE on device_alert_state.
        svc.cleanup_device("dev-clean")
        svc.cleanup_device("nonexistent-device")
