"""Device connection registry — tracks live WebSocket connections."""

import logging
from datetime import datetime, timezone
from typing import Optional

from fastapi import WebSocket

logger = logging.getLogger("agora.cms.device_manager")


class ConnectedDevice:
    def __init__(self, device_id: str, websocket: WebSocket):
        self.device_id = device_id
        self.websocket = websocket
        self.connected_at = datetime.now(timezone.utc)

    async def send_json(self, data: dict):
        await self.websocket.send_json(data)


class DeviceManager:
    """In-memory registry of connected devices."""

    def __init__(self):
        self._connections: dict[str, ConnectedDevice] = {}

    def register(self, device_id: str, websocket: WebSocket) -> ConnectedDevice:
        conn = ConnectedDevice(device_id, websocket)
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


# Singleton — shared across the application
device_manager = DeviceManager()
