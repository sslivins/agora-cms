"""Tests for the trimmed-down local connection registry.

After Stage 2c (#344), :class:`cms.services.device_manager.DeviceManager`
only tracks the per-replica WebSocket sockets this process owns plus the
synchronous-log-RPC future map.  Presence + telemetry live in Postgres
and are exercised in :mod:`tests.test_device_presence`.
"""

import pytest

from cms.services.device_manager import DeviceManager


class TestDeviceManager:
    def test_initial_state(self):
        dm = DeviceManager()
        assert dm.is_connected("anything") is False

    def test_register_and_query(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        ws = FakeWS()
        dm.register("device-1", ws)
        assert dm.is_connected("device-1")
        assert not dm.is_connected("device-2")

    def test_disconnect(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("device-1", FakeWS())
        dm.disconnect("device-1")
        assert not dm.is_connected("device-1")

    def test_disconnect_nonexistent(self):
        dm = DeviceManager()
        # Should not raise
        dm.disconnect("nonexistent")

    def test_get(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("device-1", FakeWS())
        conn = dm.get("device-1")
        assert conn is not None
        assert conn.device_id == "device-1"

    def test_get_nonexistent(self):
        dm = DeviceManager()
        assert dm.get("nonexistent") is None

    def test_ip_address_stored_on_connection(self):
        """``ip_address`` is kept on the local connection for the
        direct-WS path to hand to :func:`device_presence.mark_online`."""
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-1", FakeWS(), ip_address="192.168.1.100")
        conn = dm.get("dev-1")
        assert conn.ip_address == "192.168.1.100"

    def test_multiple_devices(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("d1", FakeWS())
        dm.register("d2", FakeWS())
        dm.register("d3", FakeWS())
        assert all(dm.is_connected(d) for d in ("d1", "d2", "d3"))

        dm.disconnect("d2")
        assert not dm.is_connected("d2")
        assert dm.is_connected("d1")
        assert dm.is_connected("d3")


class TestSendToDevice:
    @pytest.mark.asyncio
    async def test_unknown_device_returns_false(self):
        dm = DeviceManager()
        ok = await dm.send_to_device("nobody", {"type": "ping"})
        assert ok is False

    @pytest.mark.asyncio
    async def test_send_removes_broken_connection(self):
        """A send that raises should drop the connection so subsequent
        sends don't keep hitting the dead socket."""
        dm = DeviceManager()

        class ExplodingWS:
            async def send_json(self, _payload):
                raise RuntimeError("boom")

        dm.register("dev-x", ExplodingWS())
        ok = await dm.send_to_device("dev-x", {"type": "ping"})
        assert ok is False
        assert not dm.is_connected("dev-x")
