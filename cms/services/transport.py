"""Abstract device transport — hides WS-vs-WPS from app code.

Stage 1 of the multi-replica refactor (see
``docs/multi-replica-architecture.md`` on the ``docs/multi-replica-plan``
branch, and issue #344) introduced this interface; Stage 2c switched
presence + telemetry over to the ``devices`` table so every replica
sees the same view.  Presence queries (``is_connected``,
``connected_count``, ``connected_ids``, ``get_all_states``) now hit
the DB — they are ``async`` and open a short-lived session from the
configured session factory when the caller doesn't supply one.

WS-lifecycle operations (``register``/``disconnect``/``update_status``/
``resolve_log_request``) remain implementation details of the direct-WS
connection registry on ``device_manager`` — only ``cms/routers/ws.py``
and the unit tests that fabricate device connections import it
directly.  Other code touches presence through this interface.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from sqlalchemy.ext.asyncio import AsyncSession

from cms.services import device_presence


@asynccontextmanager
async def _session() -> AsyncIterator[AsyncSession]:
    """Open a short-lived session from the app-wide factory.

    Kept inside this module so the transport doesn't leak SQLAlchemy
    imports up to the callers.  The factory is populated by
    :func:`shared.database.init_db` at app startup and by the test
    ``app`` fixture at test time.
    """
    from cms.database import get_session_factory
    factory = get_session_factory()
    if factory is None:
        raise RuntimeError(
            "Session factory is not initialised — did init_db() run? "
            "(tests must use the 'app' fixture, not call the transport directly.)"
        )
    async with factory() as session:
        yield session


class DeviceTransport(ABC):
    """Application-level surface for device send + presence + state.

    Every method is expected to be safe to call from any CMS replica —
    presence + telemetry live in Postgres, message delivery is handled
    by the concrete implementation.
    """

    @abstractmethod
    async def send_to_device(self, device_id: str, message: dict[str, Any]) -> bool:
        """Send a JSON message to a single device.

        Returns ``True`` if the message was handed off to the transport,
        ``False`` if the device is not reachable from this replica (or,
        in the WPS transport, not currently connected to any replica)."""

    @abstractmethod
    async def is_connected(self, device_id: str) -> bool:
        """Whether the device has a live connection right now.

        DB-backed since Stage 2c — answers across replicas."""

    @abstractmethod
    async def connected_count(self) -> int:
        """Total number of currently-connected devices."""

    @abstractmethod
    async def connected_ids(self) -> list[str]:
        """IDs of all currently-connected devices."""

    @abstractmethod
    async def get_all_states(self) -> list[dict[str, Any]]:
        """Latest playback/health state for every connected device."""

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
    async def dispatch_request_logs(
        self,
        device_id: str,
        *,
        request_id: str,
        services: list[str] | None = None,
        since: str = "24h",
    ) -> None:
        """Send a ``request_logs`` command without awaiting the reply.

        Stage 3b's async path: the outbox row owns the state machine;
        the transport just fires the message and returns.  Raises
        :class:`ValueError` on transport failure (device offline or WS
        send error) so the caller can bump ``attempts`` / record the
        error in the outbox."""

    @abstractmethod
    async def set_state_flags(self, device_id: str, **flags: Any) -> None:
        """Optimistically update flag columns on the device row.

        Used by toggle endpoints (SSH, local-api) so the UI reflects the
        new value before the next STATUS heartbeat arrives."""


class LocalDeviceTransport(DeviceTransport):
    """Direct-WebSocket transport.

    Sends go through the in-process ``device_manager`` socket registry
    (the WebSocket this replica owns is the only one that can push a
    message to a given device); presence + telemetry live in Postgres
    so the *read* side is consistent across replicas.
    """

    def __init__(self, manager: Any | None = None) -> None:
        if manager is None:
            from cms.services.device_manager import device_manager as _dm
            manager = _dm
        self._manager = manager

    async def send_to_device(self, device_id: str, message: dict[str, Any]) -> bool:
        ok = await self._manager.send_to_device(device_id, message)
        # If the send blew up and the local socket was dropped, clear
        # the presence flag in the DB too — otherwise stale online=true
        # can persist across replicas until the next heartbeat loop.
        if not ok and not self._manager.is_connected(device_id):
            async with _session() as db:
                await device_presence.mark_offline(db, device_id)
        return ok

    async def is_connected(self, device_id: str) -> bool:
        # Local transport is single-replica so a live socket on this
        # process is just as authoritative as ``devices.online`` —
        # treat the two as a union so tests that exercise the direct-WS
        # path (``device_manager.register`` + no ``mark_online``) still
        # see the device as connected.  Production code always pairs
        # register with mark_online anyway.
        if self._manager.is_connected(device_id):
            return True
        async with _session() as db:
            return await device_presence.is_online(db, device_id)

    async def connected_count(self) -> int:
        async with _session() as db:
            db_count = await device_presence.count_online(db)
            ids = await device_presence.ids_online(db)
        local_only = [d for d in self._manager._connections if d not in ids]
        return db_count + len(local_only)

    async def connected_ids(self) -> list[str]:
        async with _session() as db:
            db_ids = await device_presence.ids_online(db)
        merged = list(db_ids)
        for d in self._manager._connections:
            if d not in merged:
                merged.append(d)
        return merged

    async def get_all_states(self) -> list[dict[str, Any]]:
        async with _session() as db:
            return await device_presence.list_states(db)

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

    async def dispatch_request_logs(
        self,
        device_id: str,
        *,
        request_id: str,
        services: list[str] | None = None,
        since: str = "24h",
    ) -> None:
        # Look up the live WS directly — this transport is single-replica
        # so if we don't own a socket, nobody does.  Do NOT register a
        # future in ``_pending_log_requests``: the outbox owns state.
        conn = self._manager.get(device_id)
        if conn is None:
            raise ValueError(f"Device {device_id} is not connected")

        from cms.schemas.protocol import RequestLogsMessage
        msg = RequestLogsMessage(
            request_id=request_id, services=services, since=since,
        )
        try:
            await conn.send_json(msg.model_dump(mode="json"))
        except Exception as exc:
            raise ValueError(
                f"Failed to send request to device {device_id}: {exc}"
            ) from exc

    async def set_state_flags(self, device_id: str, **flags: Any) -> None:
        async with _session() as db:
            await device_presence.set_flags(db, device_id, **flags)


# ──────────────────────────────────────────────────────────────────────
# Module-level singleton + getter/setter
# ──────────────────────────────────────────────────────────────────────
#
# Call sites do::
#
#     get_transport().send_to_device(...)
#
# Tests may replace the backing implementation with ``set_transport()``.
# ──────────────────────────────────────────────────────────────────────

_transport: DeviceTransport = LocalDeviceTransport()


def get_transport() -> DeviceTransport:
    """Return the currently-installed device transport.

    Always returns the latest instance installed by :func:`set_transport`
    — this is what new code should call.
    """
    return _transport


def set_transport(impl: DeviceTransport) -> None:
    """Install *impl* as the process-wide device transport.

    Tests swap in a stub with :func:`set_transport` and restore the
    default with :func:`reset_transport_to_local`.
    """
    global _transport
    _transport = impl


def reset_transport_to_local() -> LocalDeviceTransport:
    """Reinstall a fresh ``LocalDeviceTransport`` — used by test fixtures."""
    t = LocalDeviceTransport()
    set_transport(t)
    return t
