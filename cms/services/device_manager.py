"""Device WebSocket connection registry.

After Stage 2c (#344), this module only tracks per-replica ephemeral
state — the live WebSocket objects this process owns and the
``_pending_log_requests`` future map for synchronous log RPCs.

Presence (``online``), identity (``connection_id``), and telemetry
(mode/asset/pipeline_state/cpu_temp_c/…) live on the ``devices`` table
and are read via :mod:`cms.services.device_presence`.  ``ConnectedDevice``
used to cache those fields too; that cache is now the DB row.
"""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger("agora.cms.device_manager")


class ConnectedDevice:
    """One local WebSocket connection owned by this replica.

    Only the handful of fields we need to actually *use* the socket
    (send frames, log diagnostics) live here.  Everything else that a
    consumer might read across replicas is in Postgres.
    """

    def __init__(
        self,
        device_id: str,
        websocket: WebSocket | None = None,
        ip_address: Optional[str] = None,
    ):
        self.device_id = device_id
        self.websocket = websocket
        self.ip_address = ip_address
        # Populated for WPS-backed connections; None on the direct-WS
        # path where no stable id is exposed.
        self.connection_id: Optional[str] = None
        self.connected_at = datetime.now(timezone.utc)

    async def send_json(self, data: dict):
        if self.websocket is None:
            # Ghost entries (no local socket) can't send directly — the
            # caller must route via transport.send_to_device instead.
            raise RuntimeError(
                f"device {self.device_id} has no local WebSocket — "
                "route via transport.send_to_device instead",
            )
        await self.websocket.send_json(data)


class DeviceManager:
    """Per-replica registry of live WebSockets.

    Presence lives in Postgres; what's here is strictly the machinery
    this process needs to push bytes onto a socket it owns.
    """

    def __init__(self):
        self._connections: dict[str, ConnectedDevice] = {}
        self._pending_log_requests: dict[str, asyncio.Future] = {}

    def register(
        self,
        device_id: str,
        websocket: WebSocket,
        ip_address: Optional[str] = None,
    ) -> ConnectedDevice:
        conn = ConnectedDevice(device_id, websocket, ip_address=ip_address)
        self._connections[device_id] = conn
        logger.info(
            "Device %s connected (%d local socket(s))",
            device_id, len(self._connections),
        )
        return conn

    def disconnect(self, device_id: str):
        self._connections.pop(device_id, None)
        logger.info(
            "Device %s disconnected (%d local socket(s))",
            device_id, len(self._connections),
        )

    def get(self, device_id: str) -> Optional[ConnectedDevice]:
        return self._connections.get(device_id)

    def is_connected(self, device_id: str) -> bool:
        """Whether this replica owns a live WebSocket for *device_id*.

        Callers that want cross-replica presence should query the
        transport (which reads ``devices.online``) — this method is
        only for replica-local checks such as "should I fan out the
        send on this replica?".
        """
        return device_id in self._connections

    async def send_to_device(self, device_id: str, message: dict) -> bool:
        conn = self._connections.get(device_id)
        if conn:
            try:
                await conn.send_json(message)
                return True
            except Exception:
                logger.warning("Failed to send to device %s", device_id)
                self.disconnect(device_id)
        return False

    async def broadcast(self, message: dict):
        for device_id in list(self._connections.keys()):
            await self.send_to_device(device_id, message)

    async def request_logs(
        self,
        device_id: str,
        services: list[str] | None = None,
        since: str = "24h",
        timeout: float = 30.0,
    ) -> dict:
        """Send a request_logs command to a device and await its response.

        Returns the logs dict ``{service_name: log_text}`` or raises
        TimeoutError/ValueError.  The awaiting future lives on this
        replica — Stage 3 replaces this with a blob-upload outbox.
        """
        conn = self._connections.get(device_id)
        if not conn:
            raise ValueError(f"Device {device_id} is not connected")

        request_id = str(uuid.uuid4())
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending_log_requests[request_id] = fut

        from cms.schemas.protocol import RequestLogsMessage
        msg = RequestLogsMessage(
            request_id=request_id,
            services=services,
            since=since,
        )
        try:
            await conn.send_json(msg.model_dump(mode="json"))
        except Exception:
            self._pending_log_requests.pop(request_id, None)
            raise ValueError(f"Failed to send request to device {device_id}")

        try:
            result = await asyncio.wait_for(fut, timeout=timeout)
            return result
        except asyncio.TimeoutError:
            self._pending_log_requests.pop(request_id, None)
            raise TimeoutError(
                f"Device {device_id} did not respond within {timeout}s",
            )

    def resolve_log_request(
        self,
        request_id: str,
        logs: dict[str, str],
        error: str | None = None,
    ):
        """Resolve a pending ``request_logs`` future.

        Called by :func:`cms.services.device_inbound.dispatch_device_message`
        when a ``logs_response`` frame arrives — the WPS webhook and the
        direct-WS paths both dispatch through that function.
        """
        fut = self._pending_log_requests.pop(request_id, None)
        if fut and not fut.done():
            if error:
                fut.set_exception(RuntimeError(error))
            else:
                fut.set_result(logs)


# Singleton — shared across the application
device_manager = DeviceManager()
