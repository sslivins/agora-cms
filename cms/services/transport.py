"""Abstract device transport — hides WS-vs-WPS from app code.

Stage 1 of the multi-replica refactor (see
``docs/multi-replica-architecture.md`` on the ``docs/multi-replica-plan``
branch, and issue #344).

Today, all call sites that need to talk to devices or query presence
import this module's ``transport`` singleton.  The current
``LocalDeviceTransport`` implementation wraps the in-process
``device_manager`` (WebSockets owned by this process) — behaviour is
identical to what the call sites did before Stage 1.

In Stage 2, a ``WPSTransport`` sibling will land that sends via Azure
Web PubSub REST and tracks presence through webhook-updated DB rows;
at that point the singleton will be chosen by config at startup.

WS-lifecycle operations (``register``/``disconnect``/``update_status``/
``resolve_log_request``) are intentionally NOT on this interface — they
are implementation details of the local direct-WS connection registry
and live on ``device_manager``.  Only ``cms/routers/ws.py`` (and unit
tests that fabricate device connections) should import
``device_manager`` directly.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class DeviceTransport(ABC):
    """Application-level surface for device send + presence + state.

    Every method is expected to be safe to call from any CMS replica.
    """

    @abstractmethod
    async def send_to_device(self, device_id: str, message: dict[str, Any]) -> bool:
        """Send a JSON message to a single device.

        Returns ``True`` if the message was handed off to the transport,
        ``False`` if the device is not reachable from this replica (or,
        in Stage 2, not currently connected to any replica)."""

    @abstractmethod
    def is_connected(self, device_id: str) -> bool:
        """Whether the device has a live connection right now."""

    @property
    @abstractmethod
    def connected_count(self) -> int:
        """Total number of currently-connected devices."""

    @property
    @abstractmethod
    def connected_ids(self) -> list[str]:
        """IDs of all currently-connected devices."""

    @abstractmethod
    def get_all_states(self) -> list[dict[str, Any]]:
        """Latest playback/health state for every connected device.

        Stage 2 will replace the in-memory implementation with a DB read
        so the data is visible across replicas."""

    @abstractmethod
    async def request_logs(
        self,
        device_id: str,
        services: list[str] | None = None,
        since: str = "24h",
        timeout: float = 30.0,
    ) -> dict[str, str]:
        """Synchronous RPC to pull device logs.

        Stage 3 replaces this with a blob-upload outbox; this method will
        be removed from the interface at that point."""

    @abstractmethod
    def set_state_flags(self, device_id: str, **flags: Any) -> None:
        """Optimistically update fields on the in-memory device state.

        Used by toggle endpoints (SSH, local-api) so the UI reflects the
        new value before the next STATUS heartbeat arrives.  No-op if
        the device is not connected.  Stage 2 replaces the in-memory
        cache with a DB UPDATE."""


class LocalDeviceTransport(DeviceTransport):
    """Direct-WebSocket transport backed by the in-process
    ``device_manager``.  This is the behaviour the CMS had before
    Stage 1 — the class exists to put a stable interface in front of it
    so Stage 2 can introduce ``WPSTransport`` without touching call
    sites."""

    def __init__(self, manager: Any | None = None) -> None:
        if manager is None:
            from cms.services.device_manager import device_manager as _dm
            manager = _dm
        self._manager = manager

    async def send_to_device(self, device_id: str, message: dict[str, Any]) -> bool:
        return await self._manager.send_to_device(device_id, message)

    def is_connected(self, device_id: str) -> bool:
        return self._manager.is_connected(device_id)

    @property
    def connected_count(self) -> int:
        return self._manager.connected_count

    @property
    def connected_ids(self) -> list[str]:
        return list(self._manager.connected_ids)

    def get_all_states(self) -> list[dict[str, Any]]:
        return self._manager.get_all_states()

    async def request_logs(
        self,
        device_id: str,
        services: list[str] | None = None,
        since: str = "24h",
        timeout: float = 30.0,
    ) -> dict[str, str]:
        return await self._manager.request_logs(
            device_id, services=services, since=since, timeout=timeout,
        )

    def set_state_flags(self, device_id: str, **flags: Any) -> None:
        conn = self._manager.get(device_id)
        if conn is None:
            return
        for key, value in flags.items():
            setattr(conn, key, value)


transport: DeviceTransport = LocalDeviceTransport()
"""Process-wide device transport — retained as a module attribute for
backwards compatibility.  Prefer ``get_transport()`` / ``set_transport()``
at new call sites: those work even after the lifespan startup has
swapped in a different implementation (e.g., ``WPSTransport``).

Import as ``from cms.services.transport import get_transport`` and use
``get_transport().send_to_device(...)``.  Tests may replace the backing
instance with :func:`set_transport` and restore with
:func:`reset_transport_to_local`.
"""


def set_transport(t: DeviceTransport) -> None:
    """Install *t* as the process-wide device transport.

    Updates both the ``transport`` module attribute and the accessor's
    backing instance so importers that captured the singleton at import
    time (legacy pattern) continue to work.
    """
    global transport
    transport = t


def get_transport() -> DeviceTransport:
    """Return the currently-installed device transport.

    Always returns the latest instance installed by :func:`set_transport`
    — this is what new code should call.
    """
    return transport


def reset_transport_to_local() -> LocalDeviceTransport:
    """Reinstall a fresh ``LocalDeviceTransport`` — used by test fixtures."""
    t = LocalDeviceTransport()
    set_transport(t)
    return t
