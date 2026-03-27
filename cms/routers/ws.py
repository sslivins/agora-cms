"""WebSocket endpoint for device connections."""

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.database import get_db
from cms.models.device import Device, DeviceStatus
from cms.schemas.protocol import (
    PROTOCOL_VERSION,
    MessageType,
    SyncMessage,
)
from cms.services.device_manager import device_manager

logger = logging.getLogger("agora.cms.ws")

router = APIRouter()


@router.websocket("/ws/device")
async def device_websocket(websocket: WebSocket, db: AsyncSession = Depends(get_db)):
    await websocket.accept()
    device_id = None

    try:
        # ── 1. Wait for register message ──
        raw = await websocket.receive_json()

        if raw.get("type") != MessageType.REGISTER:
            await websocket.send_json({"error": "Expected register message"})
            await websocket.close(code=4001)
            return

        device_id = raw.get("device_id")
        if not device_id:
            await websocket.send_json({"error": "Missing device_id"})
            await websocket.close(code=4002)
            return

        protocol = raw.get("protocol_version", 0)
        if protocol != PROTOCOL_VERSION:
            await websocket.send_json({
                "error": f"Protocol mismatch: expected {PROTOCOL_VERSION}, got {protocol}"
            })
            await websocket.close(code=4003)
            return

        # ── 2. Upsert device in database ──
        result = await db.execute(select(Device).where(Device.id == device_id))
        device = result.scalar_one_or_none()

        if device is None:
            # New device — create as pending
            device = Device(
                id=device_id,
                name=raw.get("device_id", ""),
                status=DeviceStatus.PENDING,
                firmware_version=raw.get("firmware_version", ""),
                storage_capacity_mb=raw.get("storage_capacity_mb", 0),
                storage_used_mb=raw.get("storage_used_mb", 0),
                last_seen=datetime.now(timezone.utc),
            )
            db.add(device)
            await db.commit()
            await db.refresh(device)
            logger.info("New device registered: %s (pending approval)", device_id)
        else:
            # Existing device — update stats
            device.firmware_version = raw.get("firmware_version", device.firmware_version)
            device.storage_capacity_mb = raw.get("storage_capacity_mb", device.storage_capacity_mb)
            device.storage_used_mb = raw.get("storage_used_mb", device.storage_used_mb)
            device.last_seen = datetime.now(timezone.utc)
            await db.commit()

        # ── 3. Register connection ──
        device_manager.register(device_id, websocket)

        # ── 4. Send sync ──
        sync = SyncMessage()
        await websocket.send_json(sync.model_dump(mode="json"))

        # ── 5. Message loop ──
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == MessageType.STATUS:
                device.last_seen = datetime.now(timezone.utc)
                device.storage_used_mb = msg.get("storage_used_mb", device.storage_used_mb)
                await db.commit()

            elif msg_type == MessageType.ASSET_ACK:
                logger.info("Device %s confirmed asset: %s", device_id, msg.get("asset_name"))

            elif msg_type == MessageType.ASSET_DELETED:
                logger.info("Device %s deleted asset: %s", device_id, msg.get("asset_name"))

            else:
                logger.warning("Unknown message type from %s: %s", device_id, msg_type)

    except WebSocketDisconnect:
        logger.info("Device %s disconnected", device_id)
    except Exception as e:
        logger.error("WebSocket error for %s: %s", device_id, e)
    finally:
        if device_id:
            device_manager.disconnect(device_id)
