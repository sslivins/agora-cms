"""Device connection registry — tracks live WebSocket connections."""

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger("agora.cms.device_manager")


class ConnectedDevice:
    def __init__(self, device_id: str, websocket: WebSocket | None = None, ip_address: Optional[str] = None):
        self.device_id = device_id
        self.websocket = websocket
        self.ip_address = ip_address
        # Populated for WPS-backed connections (webhook path); None on the
        # direct-WS path where no stable id is exposed.
        self.connection_id: Optional[str] = None
        self.connected_at = datetime.now(timezone.utc)
        # Playback state from last STATUS message
        self.mode: str = "unknown"
        self.asset: Optional[str] = None
        self.pipeline_state: str = "NULL"
        self.started_at: Optional[str] = None
        self.playback_position_ms: Optional[int] = None
        self.uptime_seconds: int = 0
        self.cpu_temp_c: Optional[float] = None
        self.ssh_enabled: Optional[bool] = None
        self.local_api_enabled: Optional[bool] = None
        self.display_connected: Optional[bool] = None
        # Error state from last STATUS message
        self.error: Optional[str] = None
        self.error_since: Optional[datetime] = None

    async def send_json(self, data: dict):
        if self.websocket is None:
            # Ghost entries created by the WPS webhook path have no local
            # socket — callers must route via transport.send_to_device.
            # Raising here catches any site that hasn't been migrated.
            raise RuntimeError(
                f"device {self.device_id} has no local WebSocket — "
                "route via transport.send_to_device instead",
            )
        await self.websocket.send_json(data)


class DeviceManager:
    """In-memory registry of connected devices."""

    def __init__(self):
        self._connections: dict[str, ConnectedDevice] = {}
        self._pending_log_requests: dict[str, asyncio.Future] = {}

    def register(self, device_id: str, websocket: WebSocket, ip_address: Optional[str] = None) -> ConnectedDevice:
        conn = ConnectedDevice(device_id, websocket, ip_address=ip_address)
        self._connections[device_id] = conn
        logger.info("Device %s connected (%d total)", device_id, len(self._connections))
        return conn

    def register_remote(
        self,
        device_id: str,
        connection_id: str,
        ip_address: Optional[str] = None,
    ) -> ConnectedDevice:
        """Register a device whose transport is remote (Web PubSub).

        No local WebSocket is associated — sends must go through the
        transport (``WPSTransport.send_to_device`` → REST call to the
        broker/WPS).  The in-memory entry still tracks playback state so
        the existing UI / presence queries keep working from this replica.

        Stage 2c will migrate this in-memory state to the DB so presence
        is visible across all replicas; today it intentionally keeps the
        same single-replica behaviour as the direct-WS path.
        """
        conn = ConnectedDevice(device_id, websocket=None, ip_address=ip_address)
        conn.connection_id = connection_id
        self._connections[device_id] = conn
        logger.info(
            "Device %s connected via WPS (connection_id=%s, %d total)",
            device_id, connection_id, len(self._connections),
        )
        return conn

    def disconnect(self, device_id: str):
        self._connections.pop(device_id, None)
        logger.info("Device %s disconnected (%d total)", device_id, len(self._connections))

    def get(self, device_id: str) -> Optional[ConnectedDevice]:
        return self._connections.get(device_id)

    def is_connected(self, device_id: str) -> bool:
        return device_id in self._connections

    @property
    def connected_count(self) -> int:
        return len(self._connections)

    @property
    def connected_ids(self) -> list[str]:
        return list(self._connections.keys())

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

    def update_status(
        self,
        device_id: str,
        mode: str,
        asset: str | None,
        uptime_seconds: int = 0,
        cpu_temp_c: float | None = None,
        error: str | None = None,
        pipeline_state: str = "NULL",
        started_at: str | None = None,
        playback_position_ms: int | None = None,
        ssh_enabled: bool | None = None,
        local_api_enabled: bool | None = None,
        display_connected: bool | None = None,
    ):
        conn = self._connections.get(device_id)
        if conn:
            conn.mode = mode
            conn.asset = asset
            conn.pipeline_state = pipeline_state
            conn.started_at = started_at
            conn.playback_position_ms = playback_position_ms
            conn.uptime_seconds = uptime_seconds
            conn.cpu_temp_c = cpu_temp_c
            if ssh_enabled is not None:
                conn.ssh_enabled = ssh_enabled
            if local_api_enabled is not None:
                conn.local_api_enabled = local_api_enabled
            conn.display_connected = display_connected
            if error and not conn.error:
                conn.error_since = datetime.now(timezone.utc)
            elif not error:
                conn.error_since = None
            conn.error = error

    async def request_logs(
        self,
        device_id: str,
        services: list[str] | None = None,
        since: str = "24h",
        timeout: float = 30.0,
    ) -> dict:
        """Send a request_logs command to a device and wait for the response.

        Returns the logs dict {service_name: log_text} or raises TimeoutError/ValueError.
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
            raise TimeoutError(f"Device {device_id} did not respond within {timeout}s")

    def resolve_log_request(self, request_id: str, logs: dict[str, str], error: str | None = None):
        """Called by the WS handler when a logs_response arrives."""
        fut = self._pending_log_requests.pop(request_id, None)
        if fut and not fut.done():
            if error:
                fut.set_exception(RuntimeError(error))
            else:
                fut.set_result(logs)

    def get_all_states(self) -> list[dict]:
        return [
            {
                "device_id": c.device_id,
                "mode": c.mode,
                "asset": c.asset,
                "pipeline_state": c.pipeline_state,
                "started_at": c.started_at,
                "playback_position_ms": c.playback_position_ms,
                "uptime_seconds": c.uptime_seconds,
                "connected_at": c.connected_at.isoformat(),
                "cpu_temp_c": c.cpu_temp_c,
                "ip_address": c.ip_address,
                "error": c.error,
                "error_since": c.error_since.isoformat() if c.error_since else None,
                "ssh_enabled": c.ssh_enabled,
                "local_api_enabled": c.local_api_enabled,
                "display_connected": c.display_connected,
            }
            for c in self._connections.values()
        ]


# Singleton — shared across the application
device_manager = DeviceManager()
