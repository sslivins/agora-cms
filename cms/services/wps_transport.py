"""Azure Web PubSub device transport.

Implements :class:`cms.services.transport.DeviceTransport` on top of the
async Web PubSub service SDK.  Outbound messages are HTTP POSTs to the
WPS REST API (``/api/hubs/{hub}/users/{userId}/:send``); inbound
messages arrive via the upstream webhook receiver at
``/internal/wps/events`` (see :mod:`cms.routers.wps_webhook`) which
registers presence and dispatches each payload through
``dispatch_device_message``.

Presence + telemetry live in Postgres since Stage 2c (#344) — read-side
helpers are shared with ``LocalDeviceTransport`` via
:mod:`cms.services.device_presence`.  The only per-replica state the
WPS path still keeps in-memory is the ``_pending_log_requests`` future
map used by the synchronous logs RPC (see Stage 3 for the outbox
replacement).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from azure.core.exceptions import HttpResponseError
from azure.messaging.webpubsubservice.aio import WebPubSubServiceClient

from cms.services import device_presence
from cms.services.transport import DeviceTransport, _session

logger = logging.getLogger("agora.cms.wps_transport")


class WPSTransport(DeviceTransport):
    """Transport backed by Azure Web PubSub.

    Outbound sends go via the async SDK (``send_to_user``); presence and
    state are read from Postgres so every replica sees the same view.
    The webhook receiver keeps ``devices.online`` and the telemetry
    columns fresh via :mod:`cms.services.device_presence`.
    """

    def __init__(
        self,
        connection_string: str,
        hub: str,
        device_manager: Any | None = None,
    ) -> None:
        if device_manager is None:
            from cms.services.device_manager import device_manager as _dm
            device_manager = _dm
        # Only still needed for ``_pending_log_requests`` — the log RPC
        # is replica-local by nature (the awaiting future lives here).
        self._manager = device_manager
        self._hub = hub
        self._client: WebPubSubServiceClient = (
            WebPubSubServiceClient.from_connection_string(
                connection_string, hub=hub,
            )
        )

    # ---- outbound -----------------------------------------------------

    async def send_to_device(self, device_id: str, message: dict[str, Any]) -> bool:
        """Send a JSON payload to a single device via WPS REST.

        Returns ``True`` on success.  Azure returns 404 when the user has
        no active connections; translate that to ``False`` to match
        :class:`LocalDeviceTransport`'s semantics (no per-caller
        distinction between "unknown device" and "known but offline" —
        both are "can't reach it").  All other errors are logged and
        swallowed into ``False`` for the same reason.
        """
        try:
            await self._client.send_to_user(
                device_id,
                json.dumps(message),
                content_type="application/json",
            )
            return True
        except HttpResponseError as e:
            status = getattr(e, "status_code", None)
            if status == 404:
                logger.debug("send_to_device(%s): user has no active connections", device_id)
                # WPS says "no connections" — clear the DB presence flag
                # so the next readerl sees the device as offline.
                try:
                    async with _session() as db:
                        await device_presence.mark_offline(db, device_id)
                except Exception:
                    logger.exception("Failed to clear presence after 404 send")
                return False
            logger.warning(
                "send_to_device(%s) failed: %s (status=%s)", device_id, e, status,
            )
            return False
        except Exception:
            logger.exception("send_to_device(%s) unexpected error", device_id)
            return False

    # ---- presence (DB-backed) -----------------------------------------

    async def is_connected(self, device_id: str) -> bool:
        async with _session() as db:
            return await device_presence.is_online(db, device_id)

    async def connected_count(self) -> int:
        async with _session() as db:
            return await device_presence.count_online(db)

    async def connected_ids(self) -> list[str]:
        async with _session() as db:
            return await device_presence.ids_online(db)

    async def get_all_states(self) -> list[dict[str, Any]]:
        async with _session() as db:
            return await device_presence.list_states(db)

    # ---- synchronous RPC (logs) ----------------------------------------

    async def request_logs(
        self,
        device_id: str,
        services: list[str] | None = None,
        since: str = "24h",
        timeout: float = 30.0,
    ) -> dict[str, str]:
        """Send a ``request_logs`` command and await the device's reply.

        The awaiting future is held in-process (per-replica) — Stage 3
        will replace this with a blob-upload outbox so logs are
        deliverable from any replica.
        """
        import asyncio
        import uuid

        from cms.schemas.protocol import RequestLogsMessage

        if not await self.is_connected(device_id):
            raise ValueError(f"Device {device_id} is not connected")

        request_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._manager._pending_log_requests[request_id] = fut

        msg = RequestLogsMessage(
            request_id=request_id, services=services, since=since,
        )
        ok = await self.send_to_device(device_id, msg.model_dump(mode="json"))
        if not ok:
            self._manager._pending_log_requests.pop(request_id, None)
            raise ValueError(f"Failed to send request to device {device_id}")

        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            self._manager._pending_log_requests.pop(request_id, None)
            raise TimeoutError(
                f"Device {device_id} did not respond within {timeout}s"
            )

    # ---- state hints ---------------------------------------------------

    async def set_state_flags(self, device_id: str, **flags: Any) -> None:
        async with _session() as db:
            await device_presence.set_flags(db, device_id, **flags)

    # ---- WPS-specific helpers -----------------------------------------

    async def get_client_access_token(
        self, device_id: str, minutes_to_expire: int = 60,
    ) -> dict[str, Any]:
        """Mint a device-scoped client access token (URL + JWT).

        Returned dict has ``url`` (ws:// ...) and ``token`` keys — hand
        both to the device and it can open the WPS client socket.
        """
        result = await self._client.get_client_access_token(
            user_id=device_id, minutes_to_expire=minutes_to_expire,
        )
        # SDK returns a MutableMapping with url/token/baseUrl.
        return dict(result)

    async def close(self) -> None:
        await self._client.close()
