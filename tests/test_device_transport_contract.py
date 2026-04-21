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
    # Stage 2c: Presence reads (``connected_count``/``connected_ids``/
    # ``is_connected``/``get_all_states``) are now async and hit the DB.
    # Coverage lives in ``tests/test_device_presence.py`` + integration
    # tests that use the ``app`` fixture.  ``set_state_flags`` is also
    # async and DB-backed.  ``update_status`` was removed from
    # ``DeviceManager`` (moved to ``device_presence.update_status``).
    #
    # The unit-testable transport surface that doesn't need a session
    # factory is send_to_device and request_logs — those remain below.

    @pytest.mark.asyncio
    async def test_send_to_device_success(self):
        t, dm = _fresh_local()
        ws = _FakeWS()
        dm.register("d1", ws)
        ok = await t.send_to_device("d1", {"type": "ping"})
        assert ok is True
        assert ws.sent == [{"type": "ping"}]

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
