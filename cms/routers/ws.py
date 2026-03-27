"""WebSocket endpoint for device connections."""

import hashlib
import logging
import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.database import get_db
from cms.models.device import Device, DeviceStatus
from cms.models.registration_token import RegistrationToken
from cms.schemas.protocol import (
    PROTOCOL_VERSION,
    AuthAssignedMessage,
    MessageType,
    SyncMessage,
)
from cms.services.device_manager import device_manager

logger = logging.getLogger("agora.cms.ws")

router = APIRouter()


def _hash_token(token: str) -> str:
    """SHA-256 hash of a token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


async def _validate_registration_token(
    reg_token: str, db: AsyncSession
) -> RegistrationToken | None:
    """Validate a registration token. Returns the token row or None."""
    result = await db.execute(
        select(RegistrationToken).where(
            RegistrationToken.token == reg_token,
            RegistrationToken.is_active == True,
        )
    )
    token = result.scalar_one_or_none()
    if token is None:
        return None
    # Check usage limit
    if token.use_count >= token.max_uses:
        return None
    # Check expiry
    if token.expires_at and datetime.now(timezone.utc) > token.expires_at:
        return None
    return token


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

        auth_token = raw.get("auth_token", "")
        if not auth_token:
            await websocket.send_json({"error": "Missing auth_token"})
            await websocket.close(code=4004)
            return

        # ── 2. Authenticate ──
        result = await db.execute(select(Device).where(Device.id == device_id))
        device = result.scalar_one_or_none()

        if device is None:
            # New device — auth_token must be a valid registration token
            reg_token = await _validate_registration_token(auth_token, db)
            if reg_token is None:
                logger.warning("Device %s rejected: invalid registration token", device_id)
                await websocket.send_json({"error": "Invalid registration token"})
                await websocket.close(code=4004)
                return

            # Consume one use of the registration token
            reg_token.use_count += 1

            # Create device as pending
            device = Device(
                id=device_id,
                name=device_id,
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

            # Generate and assign a device auth token immediately
            device_auth_token = secrets.token_urlsafe(32)
            device.device_auth_token_hash = _hash_token(device_auth_token)
            await db.commit()

            # Push the auth token to the device so it can store it for reconnect
            auth_msg = AuthAssignedMessage(device_auth_token=device_auth_token)
            await websocket.send_json(auth_msg.model_dump(mode="json"))
            logger.info("Auth token assigned to device %s", device_id)
        else:
            # Known device — auth_token must match stored hash
            if not device.device_auth_token_hash:
                # Edge case: device exists but has no auth token (legacy or manual entry)
                # Treat auth_token as a registration token to assign one
                reg_token = await _validate_registration_token(auth_token, db)
                if reg_token is None:
                    logger.warning("Device %s rejected: no stored token and invalid reg token", device_id)
                    await websocket.send_json({"error": "Invalid credentials"})
                    await websocket.close(code=4004)
                    return
                reg_token.use_count += 1
                device_auth_token = secrets.token_urlsafe(32)
                device.device_auth_token_hash = _hash_token(device_auth_token)
                device.last_seen = datetime.now(timezone.utc)
                await db.commit()
                auth_msg = AuthAssignedMessage(device_auth_token=device_auth_token)
                await websocket.send_json(auth_msg.model_dump(mode="json"))
                logger.info("Auth token assigned to existing device %s", device_id)
            else:
                # Normal reconnect — verify device auth token
                if _hash_token(auth_token) != device.device_auth_token_hash:
                    logger.warning("Device %s rejected: invalid device auth token", device_id)
                    await websocket.send_json({"error": "Invalid credentials"})
                    await websocket.close(code=4004)
                    return

            # Update device stats
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
