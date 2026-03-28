"""Tests for device manager service."""

import pytest

from cms.services.device_manager import DeviceManager


class TestDeviceManager:
    def test_initial_state(self):
        dm = DeviceManager()
        assert dm.connected_count == 0
        assert dm.connected_ids == []

    def test_register_and_query(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        ws = FakeWS()
        dm.register("device-1", ws)
        assert dm.connected_count == 1
        assert dm.is_connected("device-1")
        assert not dm.is_connected("device-2")
        assert "device-1" in dm.connected_ids

    def test_disconnect(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("device-1", FakeWS())
        dm.disconnect("device-1")
        assert dm.connected_count == 0
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

    def test_multiple_devices(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("d1", FakeWS())
        dm.register("d2", FakeWS())
        dm.register("d3", FakeWS())
        assert dm.connected_count == 3

        dm.disconnect("d2")
        assert dm.connected_count == 2
        assert not dm.is_connected("d2")
        assert dm.is_connected("d1")
        assert dm.is_connected("d3")
