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

    def test_ip_address_stored(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-1", FakeWS(), ip_address="192.168.1.100")
        conn = dm.get("dev-1")
        assert conn.ip_address == "192.168.1.100"

    def test_ip_address_defaults_to_none(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-1", FakeWS())
        conn = dm.get("dev-1")
        assert conn.ip_address is None

    def test_ip_address_in_get_all_states(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-1", FakeWS(), ip_address="10.0.0.1")
        dm.register("dev-2", FakeWS(), ip_address="10.0.0.2")
        states = {s["device_id"]: s for s in dm.get_all_states()}
        assert states["dev-1"]["ip_address"] == "10.0.0.1"
        assert states["dev-2"]["ip_address"] == "10.0.0.2"


class TestDeviceManagerErrorTracking:
    """Error state is surfaced from device status heartbeats."""

    def test_error_initially_none(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-err-1", FakeWS())
        conn = dm.get("dev-err-1")
        assert conn.error is None
        assert conn.error_since is None

    def test_error_set_on_status(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-err-2", FakeWS())
        dm.update_status("dev-err-2", mode="play", asset="test.mp4", error="Pipeline error: not-linked")
        conn = dm.get("dev-err-2")
        assert conn.error == "Pipeline error: not-linked"
        assert conn.error_since is not None

    def test_error_cleared_when_resolved(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-err-3", FakeWS())
        dm.update_status("dev-err-3", mode="play", asset="test.mp4", error="Pipeline error")
        assert dm.get("dev-err-3").error is not None

        # Next status has no error — should clear
        dm.update_status("dev-err-3", mode="play", asset="test.mp4", error=None)
        conn = dm.get("dev-err-3")
        assert conn.error is None
        assert conn.error_since is None

    def test_error_since_preserved_across_updates(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-err-4", FakeWS())
        dm.update_status("dev-err-4", mode="play", asset="test.mp4", error="Error A")
        first_since = dm.get("dev-err-4").error_since

        # Same device, still in error — error_since should NOT reset
        dm.update_status("dev-err-4", mode="play", asset="test.mp4", error="Error A")
        assert dm.get("dev-err-4").error_since == first_since

    def test_error_in_get_all_states(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-err-5", FakeWS())
        dm.update_status("dev-err-5", mode="play", asset="test.mp4", error="Pipeline error")

        states = {s["device_id"]: s for s in dm.get_all_states()}
        assert states["dev-err-5"]["error"] == "Pipeline error"
        assert states["dev-err-5"]["error_since"] is not None

    def test_no_error_in_get_all_states(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-err-6", FakeWS())
        states = {s["device_id"]: s for s in dm.get_all_states()}
        assert states["dev-err-6"]["error"] is None
        assert states["dev-err-6"]["error_since"] is None


class TestDeviceManagerPlaybackState:
    """Playback state fields are stored and surfaced from device status heartbeats."""

    def test_playback_fields_default_values(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-pb-1", FakeWS())
        conn = dm.get("dev-pb-1")
        assert conn.pipeline_state == "NULL"
        assert conn.started_at is None
        assert conn.playback_position_ms is None

    def test_playback_fields_updated_on_status(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-pb-2", FakeWS())
        dm.update_status(
            "dev-pb-2",
            mode="play",
            asset="video.mp4",
            pipeline_state="PLAYING",
            started_at="2026-04-01T12:00:00+00:00",
            playback_position_ms=30000,
        )
        conn = dm.get("dev-pb-2")
        assert conn.pipeline_state == "PLAYING"
        assert conn.started_at == "2026-04-01T12:00:00+00:00"
        assert conn.playback_position_ms == 30000

    def test_playback_fields_in_get_all_states(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-pb-3", FakeWS())
        dm.update_status(
            "dev-pb-3",
            mode="play",
            asset="video.mp4",
            pipeline_state="PLAYING",
            started_at="2026-04-01T12:00:00+00:00",
            playback_position_ms=15000,
        )
        states = {s["device_id"]: s for s in dm.get_all_states()}
        assert states["dev-pb-3"]["pipeline_state"] == "PLAYING"
        assert states["dev-pb-3"]["started_at"] == "2026-04-01T12:00:00+00:00"
        assert states["dev-pb-3"]["playback_position_ms"] == 15000

    def test_playback_position_none_when_not_playing(self):
        dm = DeviceManager()

        class FakeWS:
            pass

        dm.register("dev-pb-4", FakeWS())
        dm.update_status(
            "dev-pb-4",
            mode="splash",
            asset=None,
            pipeline_state="PLAYING",
            playback_position_ms=None,
        )
        conn = dm.get("dev-pb-4")
        assert conn.playback_position_ms is None
