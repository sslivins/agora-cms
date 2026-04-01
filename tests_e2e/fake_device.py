"""Fake Agora device — WebSocket client that simulates a Pi Zero 2 W.

Usage as a context manager inside async tests:

    async with FakeDevice("test-device-001", ws_url) as dev:
        assert dev.auth_token       # assigned by CMS on register
        assert dev.sync_message     # schedule sync received
        await dev.send_status()     # heartbeat
        msgs = dev.received         # all messages from CMS
"""

import asyncio
import json
import logging
from typing import Optional

import websockets
from websockets import State

logger = logging.getLogger("fake_device")

PROTOCOL_VERSION = 1


class FakeDevice:
    """Simulates an Agora device connecting to the CMS via WebSocket."""

    def __init__(
        self,
        device_id: str,
        ws_url: str,
        *,
        auth_token: str = "",
        firmware_version: str = "0.9.5",
        device_name: str = "",
        device_type: str = "Raspberry Pi Zero 2 W Rev 1.0",
        storage_capacity_mb: int = 500,
        storage_used_mb: int = 50,
    ):
        self.device_id = device_id
        self.ws_url = ws_url
        self.auth_token = auth_token
        self.firmware_version = firmware_version
        self.device_name = device_name
        self.device_type = device_type
        self.storage_capacity_mb = storage_capacity_mb
        self.storage_used_mb = storage_used_mb

        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self.received: list[dict] = []
        self.sync_message: Optional[dict] = None
        self.api_key: Optional[str] = None
        self._listen_task: Optional[asyncio.Task] = None

    async def connect(self):
        """Connect and send register message."""
        self.ws = await websockets.connect(self.ws_url)
        register_msg = {
            "type": "register",
            "protocol_version": PROTOCOL_VERSION,
            "device_id": self.device_id,
            "auth_token": self.auth_token,
            "firmware_version": self.firmware_version,
            "device_name": self.device_name,
            "device_name_custom": False,
            "device_type": self.device_type,
            "storage_capacity_mb": self.storage_capacity_mb,
            "storage_used_mb": self.storage_used_mb,
        }
        await self.ws.send(json.dumps(register_msg))

        # Read all initial messages from CMS (auth_assigned, sync, fetch_asset, config, etc.)
        await self._read_initial_messages()

        # Start background listener for ongoing messages
        self._listen_task = asyncio.create_task(self._listen())

    async def _read_initial_messages(self, timeout: float = 5.0):
        """Read the burst of messages CMS sends after registration."""
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=min(remaining, 0.5))
                msg = json.loads(raw)
                self._process_message(msg)
            except asyncio.TimeoutError:
                break  # No more messages in the burst

    def _process_message(self, msg: dict):
        """Categorize and store a received message."""
        self.received.append(msg)
        msg_type = msg.get("type")

        if msg_type == "auth_assigned":
            self.auth_token = msg.get("device_auth_token", "")
            logger.info("Device %s received auth token", self.device_id)
        elif msg_type == "sync":
            self.sync_message = msg
            logger.info("Device %s received sync (%d schedules)", self.device_id, len(msg.get("schedules", [])))
        elif msg_type == "config":
            if msg.get("api_key"):
                self.api_key = msg["api_key"]
                logger.info("Device %s received API key", self.device_id)
        elif msg_type == "fetch_asset":
            logger.info("Device %s told to fetch: %s", self.device_id, msg.get("asset_name"))
        elif msg_type == "play":
            logger.info("Device %s told to play: %s", self.device_id, msg.get("asset"))
        elif msg_type == "stop":
            logger.info("Device %s told to stop", self.device_id)
        elif msg_type == "reboot":
            logger.info("Device %s told to reboot", self.device_id)
        elif msg_type == "upgrade":
            logger.info("Device %s told to upgrade", self.device_id)

    async def _listen(self):
        """Background task to receive messages after initial burst."""
        try:
            while self.ws and self.ws.state == State.OPEN:
                try:
                    raw = await asyncio.wait_for(self.ws.recv(), timeout=1.0)
                    msg = json.loads(raw)
                    self._process_message(msg)
                except asyncio.TimeoutError:
                    continue
        except websockets.exceptions.ConnectionClosed:
            pass

    async def send_status(self, mode: str = "splash", asset: str = None):
        """Send a heartbeat status message."""
        msg = {
            "type": "status",
            "protocol_version": PROTOCOL_VERSION,
            "device_id": self.device_id,
            "mode": mode,
            "asset": asset,
            "uptime_seconds": 120,
            "storage_used_mb": self.storage_used_mb,
            "cpu_temp_c": 45.0,
        }
        await self.ws.send(json.dumps(msg))

    async def send_asset_ack(self, asset_name: str, checksum: str = "abc123"):
        """Acknowledge that an asset was downloaded."""
        msg = {
            "type": "asset_ack",
            "protocol_version": PROTOCOL_VERSION,
            "device_id": self.device_id,
            "asset_name": asset_name,
            "checksum": checksum,
        }
        await self.ws.send(json.dumps(msg))

    def get_messages_by_type(self, msg_type: str) -> list[dict]:
        """Get all received messages of a specific type."""
        return [m for m in self.received if m.get("type") == msg_type]

    async def wait_for_message(self, msg_type: str, timeout: float = 5.0) -> Optional[dict]:
        """Wait for a specific message type to arrive."""
        deadline = asyncio.get_event_loop().time() + timeout
        while asyncio.get_event_loop().time() < deadline:
            matches = self.get_messages_by_type(msg_type)
            if matches:
                return matches[-1]
            await asyncio.sleep(0.1)
        return None

    async def disconnect(self):
        """Close the WebSocket connection."""
        if self._listen_task:
            self._listen_task.cancel()
            try:
                await self._listen_task
            except asyncio.CancelledError:
                pass
        if self.ws and self.ws.state == State.OPEN:
            await self.ws.close()

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()
