"""Azure Web PubSub device transport.

Implements :class:`cms.services.transport.DeviceTransport` on top of the
async Web PubSub service SDK.  Outbound messages are HTTP POSTs to the
WPS REST API (``/api/hubs/{hub}/users/{userId}/:send``); inbound
messages arrive via the upstream webhook receiver at
``/internal/wps/events`` (see :mod:`cms.routers.wps_webhook`) which
registers presence and dispatches each payload through
``dispatch_device_message``.

Presence + in-memory state still live on the shared
:class:`cms.services.device_manager.DeviceManager` today — Stage 2c will
move them to the DB.  Ghost entries (websocket=None) created by
``register_remote`` let the existing UI / scheduler / alert paths work
unchanged from the replica that happened to process the webhook.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from azure.core.exceptions import HttpResponseError
from azure.messaging.webpubsubservice.aio import WebPubSubServiceClient

from cms.services.transport import DeviceTransport

logger = logging.getLogger("agora.cms.wps_transport")


class WPSTransport(DeviceTransport):
    """Transport backed by Azure Web PubSub.

    Outbound sends go via the async SDK (``send_to_user``); presence and
    state are read from the shared in-process ``DeviceManager`` which
    the webhook receiver keeps populated.
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
                return False
            logger.warning(
                "send_to_device(%s) failed: %s (status=%s)", device_id, e, status,
            )
            return False
        except Exception:
            logger.exception("send_to_device(%s) unexpected error", device_id)
            return False

    # ---- presence (delegated to the in-memory manager) -----------------

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

    # ---- synchronous RPC (logs) ----------------------------------------

    async def request_logs(
        self,
        device_id: str,
        services: list[str] | None = None,
        since: str = "24h",
        timeout: float = 30.0,
    ) -> dict[str, str]:
        """Send a ``request_logs`` command and await the device's reply.

        The manager's pending-log-request future is resolved by
        :func:`dispatch_device_message` when it processes the device's
        ``logs_response`` — that dispatcher runs for both the direct-WS
        and the WPS webhook paths, so the same wait-on-future pattern
        works here.  We can't reuse ``DeviceManager.request_logs``
        because its send step routes through the ghost entry's
        non-existent socket — we have to do the send ourselves.
        """
        import asyncio
        import uuid

        from cms.schemas.protocol import RequestLogsMessage

        if not self._manager.is_connected(device_id):
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

    def set_state_flags(self, device_id: str, **flags: Any) -> None:
        conn = self._manager.get(device_id)
        if conn is None:
            return
        for key, value in flags.items():
            setattr(conn, key, value)

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
