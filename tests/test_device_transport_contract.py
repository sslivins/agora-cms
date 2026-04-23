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
    # PR #345 retired the synchronous ``request_logs`` RPC on the
    # transport interface; the async ``dispatch_request_logs`` flow is
    # covered by ``tests/test_log_requests_api.py`` + ``tests/test_log_drainer.py``.
    # The unit-testable transport surface that doesn't need a session
    # factory is just ``send_to_device``.

    @pytest.mark.asyncio
    async def test_send_to_device_success(self):
        t, dm = _fresh_local()
        ws = _FakeWS()
        dm.register("d1", ws)
        ok = await t.send_to_device("d1", {"type": "ping"})
        assert ok is True
        assert ws.sent == [{"type": "ping"}]

    def test_transport_is_an_abc(self):
        # Cannot instantiate the abstract base
        with pytest.raises(TypeError):
            DeviceTransport()  # type: ignore[abstract]
