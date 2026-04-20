"""Tests for CPU temperature monitoring across the status pipeline.

Covers:
- DeviceManager tracking cpu_temp_c in update_status / get_all_states
- Dashboard HTML rendering of temperature warnings and critical alerts
- Dashboard JSON API including cpu_temp_c in device_states
"""

import hashlib

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cms.models.device import Device, DeviceStatus
from cms.services.device_manager import DeviceManager, device_manager


# ── DeviceManager unit tests ──


class TestDeviceManagerTemperature:
    def _make_dm(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("hot-device", FakeWS())
        dm.register("cool-device", FakeWS())
        return dm

    def test_cpu_temp_defaults_to_none(self):
        dm = self._make_dm()
        conn = dm.get("hot-device")
        assert conn.cpu_temp_c is None

    def test_update_status_stores_cpu_temp(self):
        dm = self._make_dm()
        dm.update_status("hot-device", mode="play", asset="video.mp4", cpu_temp_c=72.5)
        conn = dm.get("hot-device")
        assert conn.cpu_temp_c == 72.5

    def test_update_status_cpu_temp_none(self):
        dm = self._make_dm()
        dm.update_status("hot-device", mode="play", asset=None, cpu_temp_c=None)
        conn = dm.get("hot-device")
        assert conn.cpu_temp_c is None

    def test_get_all_states_includes_cpu_temp(self):
        dm = self._make_dm()
        dm.update_status("hot-device", mode="play", asset="v.mp4", cpu_temp_c=85.0)
        dm.update_status("cool-device", mode="splash", asset=None, cpu_temp_c=42.0)

        states = {s["device_id"]: s for s in dm.get_all_states()}
        assert states["hot-device"]["cpu_temp_c"] == 85.0
        assert states["cool-device"]["cpu_temp_c"] == 42.0

    def test_get_all_states_cpu_temp_none_when_not_set(self):
        dm = self._make_dm()
        states = {s["device_id"]: s for s in dm.get_all_states()}
        assert states["hot-device"]["cpu_temp_c"] is None


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


def _simulate_connected(dm, device_id, cpu_temp_c=None, mode="play"):
    """Register a fake WS connection and set its status."""

    class FakeWS:
        pass

    dm.register(device_id, FakeWS())
    dm.update_status(device_id, mode=mode, asset=None, cpu_temp_c=cpu_temp_c)


@pytest.mark.asyncio
class TestDashboardTemperature:
    """Test that high CPU temperatures surface on the dashboard."""

    async def test_normal_temp_shows_healthy(self, app, db_session, client):
        """A device at normal temperature should not trigger the Device Status alert banner."""
        await _seed_device(db_session, "dev-normal", "Normal Device")
        _simulate_connected(device_manager, "dev-normal", cpu_temp_c=45.0)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "card-danger" not in html
            assert "badge-temp-warning" not in html
            assert "badge-temp-critical" not in html
        finally:
            device_manager.disconnect("dev-normal")

    async def test_warning_temp_shows_in_device_status(self, app, db_session, client):
        """A device at 75°C should appear in the Device Status card with a warning badge."""
        await _seed_device(db_session, "dev-warm", "Warm Device")
        _simulate_connected(device_manager, "dev-warm", cpu_temp_c=75.0)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "badge-temp-warning" in html
            assert "75.0°C" in html or "75.0\u00b0C" in html
            assert "Temperature warning" in html
            assert "All devices are online and healthy" not in html
            # Card should have danger styling
            assert "card-danger" in html
        finally:
            device_manager.disconnect("dev-warm")

    async def test_critical_temp_shows_in_device_status(self, app, db_session, client):
        """A device at 85°C should appear with a critical badge and throttling message."""
        await _seed_device(db_session, "dev-hot", "Hot Device")
        _simulate_connected(device_manager, "dev-hot", cpu_temp_c=85.0)

        try:
            resp = await client.get("/")
            assert resp.status_code == 200
            html = resp.text
            assert "badge-temp-critical" in html
            assert "85.0°C" in html or "85.0\u00b0C" in html
            assert "Critical" in html
            assert "CPU throttling likely" in html
            assert "card-danger" in html
        finally:
            device_manager.disconnect("dev-hot")

    async def test_boundary_70_is_warning(self, app, db_session, client):
        """Exactly 70°C should trigger a warning (threshold is >= 70)."""
        await _seed_device(db_session, "dev-70", "Boundary Device")
        _simulate_connected(device_manager, "dev-70", cpu_temp_c=70.0)

        try:
            resp = await client.get("/")
            html = resp.text
            assert "badge-temp-warning" in html
            assert "All devices are online and healthy" not in html
        finally:
            device_manager.disconnect("dev-70")

    async def test_boundary_80_is_critical(self, app, db_session, client):
        """Exactly 80°C should trigger a critical alert (threshold is >= 80)."""
        await _seed_device(db_session, "dev-80", "Boundary Hot Device")
        _simulate_connected(device_manager, "dev-80", cpu_temp_c=80.0)

        try:
            resp = await client.get("/")
            html = resp.text
            assert "badge-temp-critical" in html
            assert "badge-temp-warning" not in html  # 80+ is critical, not warning
        finally:
            device_manager.disconnect("dev-80")

    async def test_69_is_not_warning(self, app, db_session, client):
        """69.9°C should NOT trigger a warning — just below threshold."""
        await _seed_device(db_session, "dev-69", "Almost Warm")
        _simulate_connected(device_manager, "dev-69", cpu_temp_c=69.9)

        try:
            resp = await client.get("/")
            html = resp.text
            assert "badge-temp-warning" not in html
            assert "badge-temp-critical" not in html
            assert "card-danger" not in html
        finally:
            device_manager.disconnect("dev-69")

    async def test_null_temp_not_warning(self, app, db_session, client):
        """A device with no temperature reading (None) should not trigger warnings."""
        await _seed_device(db_session, "dev-null", "No Temp Device")
        _simulate_connected(device_manager, "dev-null", cpu_temp_c=None)

        try:
            resp = await client.get("/")
            html = resp.text
            assert "badge-temp-warning" not in html
            assert "badge-temp-critical" not in html
            assert "card-danger" not in html
        finally:
            device_manager.disconnect("dev-null")


@pytest.mark.asyncio
class TestDashboardJsonTemperature:
    """Test the /api/dashboard JSON endpoint includes temperature data."""

    async def test_json_includes_cpu_temp(self, app, db_session, client):
        await _seed_device(db_session, "dev-json", "JSON Device")
        _simulate_connected(device_manager, "dev-json", cpu_temp_c=55.3)

        try:
            resp = await client.get("/api/dashboard")
            assert resp.status_code == 200
            data = resp.json()
            states = {s["id"]: s for s in data["device_states"]}
            assert "dev-json" in states
            assert states["dev-json"]["cpu_temp_c"] == 55.3
        finally:
            device_manager.disconnect("dev-json")

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

    async def test_status_message_tracks_cpu_temp(self, app, db_session):
        """Send a STATUS message with cpu_temp_c and verify device_manager stores it."""
        token = "temp-test-token"
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        device = Device(
            id="ws-temp-001",
            name="WS Temp Device",
            status=DeviceStatus.ADOPTED,
            device_auth_token_hash=token_hash,
        )
        db_session.add(device)
        await db_session.commit()
        await db_session.close()

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

                # Poll device_manager until the temperature lands (CI can be slow).
                import time
                deadline = time.time() + 5.0
                states: dict = {}
                while time.time() < deadline:
                    states = {s["device_id"]: s for s in device_manager.get_all_states()}
                    if states.get("ws-temp-001", {}).get("cpu_temp_c") == 73.2:
                        break
                    time.sleep(0.05)

                assert "ws-temp-001" in states
                assert states["ws-temp-001"]["cpu_temp_c"] == 73.2

    async def test_status_without_cpu_temp_stays_none(self, app, db_session):
        """A STATUS message without cpu_temp_c should leave it as None."""
        token = "temp-test-token2"
        token_hash = hashlib.sha256(token.encode()).hexdigest()

        device = Device(
            id="ws-temp-002",
            name="WS No Temp Device",
            status=DeviceStatus.ADOPTED,
            device_auth_token_hash=token_hash,
        )
        db_session.add(device)
        await db_session.commit()
        await db_session.close()

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

                # Poll device_manager until the device status lands. Absence of
                # cpu_temp_c means it should remain None, so wait for the mode
                # update as proof the status was processed.
                import time
                deadline = time.time() + 5.0
                states: dict = {}
                while time.time() < deadline:
                    states = {s["device_id"]: s for s in device_manager.get_all_states()}
                    if states.get("ws-temp-002", {}).get("mode") == "splash":
                        break
                    time.sleep(0.05)

                assert "ws-temp-002" in states
                assert states["ws-temp-002"]["cpu_temp_c"] is None
