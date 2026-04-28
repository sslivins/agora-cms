"""Tests for CPU temperature monitoring across the status pipeline.

Covers:
- Dashboard HTML rendering of temperature warnings and critical alerts
- Dashboard JSON API including cpu_temp_c in device_states
- End-to-end STATUS message → DB cpu_temp_c persistence

DeviceManager no longer tracks cpu_temp_c since Stage 2c (#344) — see
:mod:`tests.test_device_presence` for the DB-backed helper coverage.
"""

import hashlib

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cms.models.device import Device, DeviceStatus
from cms.services.device_manager import device_manager


# ── Dashboard integration tests ──


async def _seed_device(db_session, device_id, name, status=DeviceStatus.ADOPTED):
    """Insert a device into the test DB."""
    device = Device(
        id=device_id,
        name=name,
        status=status,
        device_auth_token_hash=hashlib.sha256(b"tok").hexdigest(),
    )
    db_session.add(device)
    await db_session.commit()
    return device


async def _simulate_connected(db_session, device_id, cpu_temp_c=None, mode="play"):
    """Register a fake WS connection and persist status in the DB.

    Stage 2c: telemetry lives in the ``devices`` row, not in the
    in-memory connection.  This helper mirrors what ``/ws/device`` does:
    local register + ``device_presence.mark_online`` + STATUS write.
    """
    from cms.services import device_presence

    class FakeWS:
        pass

    device_manager.register(device_id, FakeWS())
    await device_presence.mark_online(db_session, device_id)
    await device_presence.update_status(
        db_session, device_id,
        {"mode": mode, "asset": None, "cpu_temp_c": cpu_temp_c},
    )


async def _simulate_disconnected(db_session, device_id):
    from cms.services import device_presence
    device_manager.disconnect(device_id)
    await device_presence.mark_offline(db_session, device_id)


@pytest.mark.asyncio
class TestDashboardTemperature:
    """Test that high CPU temperatures surface on the dashboard."""

    async def test_normal_temp_shows_healthy(self, app, db_session, client):
        """A device at normal temperature should not trigger the Device Status alert banner."""
        await _seed_device(db_session, "dev-normal", "Normal Device")
        await _simulate_connected(db_session, "dev-normal", cpu_temp_c=45.0)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "card-danger" not in html
            assert "badge-temp-warning" not in html
            assert "badge-temp-critical" not in html
        finally:
            await _simulate_disconnected(db_session, "dev-normal")

    async def test_warning_temp_shows_in_device_status(self, app, db_session, client):
        """A device at 75°C should appear with a warning badge on /devices.

        Phase D rework: the dashboard no longer renders a per-device alert
        banner — it now shows stat-tile counts that link into /devices.
        Per-device temperature badges live on /devices via the row macros.
        """
        await _seed_device(db_session, "dev-warm", "Warm Device")
        await _simulate_connected(db_session, "dev-warm", cpu_temp_c=75.0)

        try:
            resp = await client.get("/devices")
            assert resp.status_code == 200
            html = resp.text
            assert "badge-temp-warning" in html
            assert "75.0°C" in html or "75.0\u00b0C" in html
        finally:
            await _simulate_disconnected(db_session, "dev-warm")

    async def test_critical_temp_shows_in_device_status(self, app, db_session, client):
        """A device at 85°C should appear with a critical badge on /devices."""
        await _seed_device(db_session, "dev-hot", "Hot Device")
        await _simulate_connected(db_session, "dev-hot", cpu_temp_c=85.0)

        try:
            resp = await client.get("/devices")
            assert resp.status_code == 200
            html = resp.text
            assert "badge-temp-critical" in html
            assert "85.0°C" in html or "85.0\u00b0C" in html
            assert "Critical" in html
        finally:
            await _simulate_disconnected(db_session, "dev-hot")

    async def test_boundary_70_is_warning(self, app, db_session, client):
        """Exactly 70°C should trigger a warning (threshold is >= 70)."""
        await _seed_device(db_session, "dev-70", "Boundary Device")
        await _simulate_connected(db_session, "dev-70", cpu_temp_c=70.0)

        try:
            resp = await client.get("/devices")
            html = resp.text
            assert "badge-temp-warning" in html
        finally:
            await _simulate_disconnected(db_session, "dev-70")

    async def test_boundary_80_is_critical(self, app, db_session, client):
        """Exactly 80°C should trigger a critical alert (threshold is >= 80)."""
        await _seed_device(db_session, "dev-80", "Boundary Hot Device")
        await _simulate_connected(db_session, "dev-80", cpu_temp_c=80.0)

        try:
            resp = await client.get("/devices")
            html = resp.text
            assert "badge-temp-critical" in html
            # /devices includes inline JS that references both badge classes
            # by name. Match a rendered chip body that includes the actual
            # temperature value to avoid colliding with JS string literals.
            assert 'badge-temp-warning">Temp 80.0' not in html  # 80+ is critical, not warning
        finally:
            await _simulate_disconnected(db_session, "dev-80")

    async def test_69_is_not_warning(self, app, db_session, client):
        """69.9°C should NOT trigger a warning — just below threshold."""
        await _seed_device(db_session, "dev-69", "Almost Warm")
        await _simulate_connected(db_session, "dev-69", cpu_temp_c=69.9)

        try:
            resp = await client.get("/")
            html = resp.text
            assert "badge-temp-warning" not in html
            assert "badge-temp-critical" not in html
            assert "card-danger" not in html
        finally:
            await _simulate_disconnected(db_session, "dev-69")

    async def test_null_temp_not_warning(self, app, db_session, client):
        """A device with no temperature reading (None) should not trigger warnings."""
        await _seed_device(db_session, "dev-null", "No Temp Device")
        await _simulate_connected(db_session, "dev-null", cpu_temp_c=None)

        try:
            resp = await client.get("/")
            html = resp.text
            assert "badge-temp-warning" not in html
            assert "badge-temp-critical" not in html
            assert "card-danger" not in html
        finally:
            await _simulate_disconnected(db_session, "dev-null")


@pytest.mark.asyncio
class TestDashboardJsonTemperature:
    """Test the /api/dashboard JSON endpoint includes temperature data."""

    async def test_json_includes_cpu_temp(self, app, db_session, client):
        await _seed_device(db_session, "dev-json", "JSON Device")
        await _simulate_connected(db_session, "dev-json", cpu_temp_c=55.3)

        try:
            resp = await client.get("/api/dashboard")
            assert resp.status_code == 200
            data = resp.json()
            states = {s["id"]: s for s in data["device_states"]}
            assert "dev-json" in states
            assert states["dev-json"]["cpu_temp_c"] == 55.3
        finally:
            await _simulate_disconnected(db_session, "dev-json")

    async def test_json_cpu_temp_null_when_offline(self, app, db_session, client):
        await _seed_device(db_session, "dev-off", "Offline Device")
        # Don't register in device_manager — device is offline

        resp = await client.get("/api/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        states = {s["id"]: s for s in data["device_states"]}
        assert "dev-off" in states
        assert states["dev-off"]["cpu_temp_c"] is None


@pytest.mark.asyncio
class TestWebSocketStatusTemperature:
    """Test that cpu_temp_c sent via WebSocket STATUS is tracked correctly."""

    async def test_status_message_tracks_cpu_temp(self, app):
        """Send a STATUS message with cpu_temp_c and verify device_manager stores it."""
        token = "temp-test-token"
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        from shared.database import get_session_factory
        factory = get_session_factory()

        async with factory() as setup_db:
            device = Device(
                id="ws-temp-001",
                name="WS Temp Device",
                status=DeviceStatus.ADOPTED,
                device_auth_token_hash=token_hash,
            )
            setup_db.add(device)
            await setup_db.commit()

        from starlette.testclient import TestClient

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                # Register
                ws.send_json({
                    "type": "register",
                    "protocol_version": 1,
                    "device_id": "ws-temp-001",
                    "auth_token": token,
                    "firmware_version": "1.0.0",
                    "storage_capacity_mb": 500,
                    "storage_used_mb": 100,
                })

                # Drain sync + config messages
                msg = ws.receive_json()
                assert msg["type"] == "sync"
                msg = ws.receive_json()
                assert msg["type"] == "config"

                # Send status with temperature
                ws.send_json({
                    "type": "status",
                    "protocol_version": 1,
                    "device_id": "ws-temp-001",
                    "mode": "play",
                    "asset": "video.mp4",
                    "uptime_seconds": 3600,
                    "storage_used_mb": 150,
                    "cpu_temp_c": 73.2,
                })

                # Poll the DB (telemetry lives there now) until the
                # STATUS heartbeat lands.  CI can be slow — give it 5s.
                import time
                from shared.database import get_session_factory
                factory = get_session_factory()
                deadline = time.time() + 5.0
                row_mode = None
                row_temp = None
                while time.time() < deadline:
                    async with factory() as probe_db:
                        r = (await probe_db.execute(
                            select(Device.mode, Device.cpu_temp_c)
                            .where(Device.id == "ws-temp-001")
                        )).one_or_none()
                        if r:
                            row_mode, row_temp = r
                            if row_temp == 73.2:
                                break
                    time.sleep(0.05)

                assert row_mode == "play"
                assert row_temp == 73.2

    async def test_status_without_cpu_temp_stays_none(self, app):
        """A STATUS message without cpu_temp_c should leave it as None."""
        token = "temp-test-token2"
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        from shared.database import get_session_factory
        factory = get_session_factory()

        async with factory() as setup_db:
            device = Device(
                id="ws-temp-002",
                name="WS No Temp Device",
                status=DeviceStatus.ADOPTED,
                device_auth_token_hash=token_hash,
            )
            setup_db.add(device)
            await setup_db.commit()

        from starlette.testclient import TestClient

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                ws.send_json({
                    "type": "register",
                    "protocol_version": 1,
                    "device_id": "ws-temp-002",
                    "auth_token": token,
                    "firmware_version": "1.0.0",
                    "storage_capacity_mb": 500,
                    "storage_used_mb": 100,
                })

                msg = ws.receive_json()
                assert msg["type"] == "sync"
                msg = ws.receive_json()
                assert msg["type"] == "config"

                # Send status WITHOUT cpu_temp_c
                ws.send_json({
                    "type": "status",
                    "protocol_version": 1,
                    "device_id": "ws-temp-002",
                    "mode": "splash",
                    "uptime_seconds": 100,
                    "storage_used_mb": 50,
                })

                # Poll DB until the STATUS heartbeat lands.  Absence of
                # cpu_temp_c means it should remain None — wait for the
                # mode update as proof the status was processed.
                import time
                from shared.database import get_session_factory
                factory = get_session_factory()
                deadline = time.time() + 5.0
                row_mode = None
                row_temp = "not-yet"
                while time.time() < deadline:
                    async with factory() as probe_db:
                        r = (await probe_db.execute(
                            select(Device.mode, Device.cpu_temp_c)
                            .where(Device.id == "ws-temp-002")
                        )).one_or_none()
                        if r:
                            row_mode, row_temp = r
                            if row_mode == "splash":
                                break
                    time.sleep(0.05)

                assert row_mode == "splash"
                assert row_temp is None
