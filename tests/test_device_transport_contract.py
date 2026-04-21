"""Contract tests for ``DeviceTransport`` implementations.

Every implementation must satisfy this suite.  Today only
``LocalDeviceTransport`` exists; Stage 2 will add ``WPSTransport`` and
will re-use this module (parametrized over impls).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest

from cms.services.device_manager import DeviceManager
from cms.services.transport import DeviceTransport, LocalDeviceTransport


@dataclass
class _FakeWS:
    sent: list[dict] = field(default_factory=list)

    async def send_json(self, data: dict) -> None:
        self.sent.append(data)


def _fresh_local() -> tuple[LocalDeviceTransport, DeviceManager]:
    """Local transport wired to a fresh DeviceManager (no shared state)."""
    dm = DeviceManager()
    return LocalDeviceTransport(manager=dm), dm


class TestLocalDeviceTransportContract:
    def test_empty_state(self):
        t, _ = _fresh_local()
        assert t.connected_count == 0
        assert t.connected_ids == []
        assert not t.is_connected("nope")
        assert t.get_all_states() == []

    def test_presence_after_register(self):
        t, dm = _fresh_local()
        dm.register("d1", _FakeWS())
        dm.register("d2", _FakeWS())
        assert t.connected_count == 2
        assert t.is_connected("d1") and t.is_connected("d2")
        assert set(t.connected_ids) == {"d1", "d2"}

    def test_get_all_states_shape(self):
        t, dm = _fresh_local()
        dm.register("d1", _FakeWS(), ip_address="10.0.0.1")
        dm.update_status("d1", mode="play", asset="a.mp4")
        states = t.get_all_states()
        assert len(states) == 1
        s = states[0]
        # Stable contract: keys the UI depends on
        for key in ("device_id", "mode", "asset", "connected_at",
                    "ip_address", "ssh_enabled", "local_api_enabled"):
            assert key in s
        assert s["device_id"] == "d1"
        assert s["ip_address"] == "10.0.0.1"

    @pytest.mark.asyncio
    async def test_send_to_device_success(self):
        t, dm = _fresh_local()
        ws = _FakeWS()
        dm.register("d1", ws)
        ok = await t.send_to_device("d1", {"type": "ping"})
        assert ok is True
        assert ws.sent == [{"type": "ping"}]

    @pytest.mark.asyncio
    async def test_send_to_unknown_device_returns_false(self):
        t, _ = _fresh_local()
        assert await t.send_to_device("missing", {"type": "ping"}) is False

    @pytest.mark.asyncio
    async def test_send_disconnects_on_error(self):
        t, dm = _fresh_local()

        class BrokenWS:
            async def send_json(self, data):
                raise RuntimeError("pipe broken")

        dm.register("d1", BrokenWS())
        assert t.is_connected("d1")
        ok = await t.send_to_device("d1", {"type": "ping"})
        assert ok is False
        # Transport surfaces the disconnection on failure.
        assert not t.is_connected("d1")

    def test_set_state_flags_updates_visible_state(self):
        t, dm = _fresh_local()
        dm.register("d1", _FakeWS())
        t.set_state_flags("d1", ssh_enabled=True, local_api_enabled=False)
        s = {x["device_id"]: x for x in t.get_all_states()}["d1"]
        assert s["ssh_enabled"] is True
        assert s["local_api_enabled"] is False

    def test_set_state_flags_on_unknown_device_is_noop(self):
        t, _ = _fresh_local()
        # Must not raise
        t.set_state_flags("ghost", ssh_enabled=True)

    @pytest.mark.asyncio
    async def test_request_logs_resolves_via_manager_hook(self):
        t, dm = _fresh_local()

        captured: list[dict] = []

        class CaptureWS:
            async def send_json(self, data):
                captured.append(data)

        dm.register("d1", CaptureWS())

        # Fire request_logs; capture the request_id from the WS message and
        # resolve it synchronously from the "device".
        import asyncio
        task = asyncio.create_task(t.request_logs("d1", services=["agora"]))
        await asyncio.sleep(0)
        # Spin briefly until the message hits CaptureWS.
        for _ in range(100):
            if captured:
                break
            await asyncio.sleep(0.01)
        assert captured, "request_logs never dispatched"
        rid = captured[0]["request_id"]

        dm.resolve_log_request(rid, logs={"agora": "hello"})
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result == {"agora": "hello"}

    def test_transport_is_an_abc(self):
        # Cannot instantiate the abstract base
        with pytest.raises(TypeError):
            DeviceTransport()  # type: ignore[abstract]
