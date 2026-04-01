"""Device connection registry — tracks live WebSocket connections."""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger("agora.cms.device_manager")


class ConnectedDevice:
    def __init__(self, device_id: str, websocket: WebSocket, ip_address: Optional[str] = None):
        self.device_id = device_id
        self.websocket = websocket
        self.ip_address = ip_address
        self.connected_at = datetime.now(timezone.utc)
        # Playback state from last STATUS message
        self.mode: str = "unknown"
        self.asset: Optional[str] = None
        self.uptime_seconds: int = 0
        self.cpu_temp_c: Optional[float] = None
        # Error state from last STATUS message
        self.error: Optional[str] = None
        self.error_since: Optional[datetime] = None

    async def send_json(self, data: dict):
        await self.websocket.send_json(data)


class DeviceManager:
    """In-memory registry of connected devices."""

    def __init__(self):
        self._connections: dict[str, ConnectedDevice] = {}

    def register(self, device_id: str, websocket: WebSocket, ip_address: Optional[str] = None) -> ConnectedDevice:
        conn = ConnectedDevice(device_id, websocket, ip_address=ip_address)
        self._connections[device_id] = conn
        logger.info("Device %s connected (%d total)", device_id, len(self._connections))
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

    def update_status(self, device_id: str, mode: str, asset: str | None, uptime_seconds: int = 0, cpu_temp_c: float | None = None, error: str | None = None):
        conn = self._connections.get(device_id)
        if conn:
            conn.mode = mode
            conn.asset = asset
            conn.uptime_seconds = uptime_seconds
            conn.cpu_temp_c = cpu_temp_c
            if error and not conn.error:
                conn.error_since = datetime.now(timezone.utc)
            elif not error:
                conn.error_since = None
            conn.error = error

    def get_all_states(self) -> list[dict]:
        return [
            {
                "device_id": c.device_id,
                "mode": c.mode,
                "asset": c.asset,
                "uptime_seconds": c.uptime_seconds,
                "connected_at": c.connected_at.isoformat(),
                "cpu_temp_c": c.cpu_temp_c,
                "ip_address": c.ip_address,
                "error": c.error,
                "error_since": c.error_since.isoformat() if c.error_since else None,
            }
            for c in self._connections.values()
        ]


# Singleton — shared across the application
device_manager = DeviceManager()
