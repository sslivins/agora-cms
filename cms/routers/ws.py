"""WebSocket endpoint for device connections."""

import hashlib
import logging
import secrets
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import get_settings
from cms.database import get_db
from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device import Device, DeviceStatus
from cms.models.device_profile import DeviceProfile
from cms.schemas.protocol import (
    PROTOCOL_VERSION,
    AuthAssignedMessage,
    ConfigMessage,
    FetchAssetMessage,
    MessageType,
    PlayMessage,
    SyncMessage,
)
from cms.services.device_manager import device_manager
from cms.services.scheduler import build_device_sync

logger = logging.getLogger("agora.cms.ws")

router = APIRouter()


def _hash_token(token: str) -> str:
    """SHA-256 hash of a token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


async def _resolve_asset_for_device(
    asset: Asset, device: Device, base_url: str, db: AsyncSession,
) -> FetchAssetMessage | None:
    """Build a FetchAssetMessage using the best variant for the device's profile.

    For video assets with a profile:
      - If a READY variant exists → use variant download URL
      - If variant exists but not ready → return None (not available yet)
    For images or devices without a profile → use source asset directly.
    """
    if asset.asset_type == AssetType.VIDEO and device.profile_id:
        result = await db.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == device.profile_id,
            )
        )
        variant = result.scalar_one_or_none()
        if variant:
            if variant.status == VariantStatus.READY:
                return FetchAssetMessage(
                    asset_name=asset.filename,
                    download_url=f"{base_url}/api/assets/variants/{variant.id}/download",
                    checksum=variant.checksum,
                    size_bytes=variant.size_bytes,
                )
            # Variant exists but not ready — skip for now
            return None

    # No profile, no variant, or image → use source
    return FetchAssetMessage(
        asset_name=asset.filename,
        download_url=f"{base_url}/api/assets/{asset.id}/download",
        checksum=asset.checksum,
        size_bytes=asset.size_bytes,
    )


# Mapping of device_type substrings to built-in profile names
_DEVICE_TYPE_PROFILE_MAP = {
    "pi zero 2 w": "pi-zero-2w",
    "raspberry pi zero 2 w": "pi-zero-2w",
}


async def _auto_assign_profile(device: Device, db: AsyncSession) -> None:
    """Auto-assign a device profile based on device_type if not already set."""
    if device.profile_id or not device.device_type:
        return

    dt_lower = device.device_type.lower()
    profile_name = None
    for pattern, name in _DEVICE_TYPE_PROFILE_MAP.items():
        if pattern in dt_lower:
            profile_name = name
            break

    if not profile_name:
        return

    result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.name == profile_name)
    )
    profile = result.scalar_one_or_none()
    if profile:
        device.profile_id = profile.id
        await db.commit()
        logger.info("Auto-assigned profile '%s' to device %s", profile_name, device.id)


async def _generate_and_push_api_key(device: Device, websocket: WebSocket, db: AsyncSession) -> None:
    """Generate a new device API key, store its hash, and push it to the device."""
    new_key = secrets.token_urlsafe(32)
    device.device_api_key_hash = _hash_token(new_key)
    device.api_key_rotated_at = datetime.now(timezone.utc)
    await db.commit()
    config_msg = ConfigMessage(api_key=new_key)
    await websocket.send_json(config_msg.model_dump(mode="json"))
    logger.info("API key pushed to device %s", device.id)


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

        # ── 2. Authenticate ──
        result = await db.execute(select(Device).where(Device.id == device_id))
        device = result.scalar_one_or_none()

        if device is None:
            # New device — create as pending
            device = Device(
                id=device_id,
                name=device_id,
                status=DeviceStatus.PENDING,
                firmware_version=raw.get("firmware_version", ""),
                device_type=raw.get("device_type", ""),
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

            # Auto-assign profile based on device_type
            await _auto_assign_profile(device, db)
        else:
            # Known device — verify auth token if device has one stored
            if device.device_auth_token_hash:
                if not auth_token or _hash_token(auth_token) != device.device_auth_token_hash:
                    logger.warning("Device %s rejected: invalid device auth token", device_id)
                    await websocket.send_json({"error": "Invalid credentials"})
                    await websocket.close(code=4004)
                    return
            else:
                # Device exists but has no auth token yet — assign one
                device_auth_token = secrets.token_urlsafe(32)
                device.device_auth_token_hash = _hash_token(device_auth_token)
                await db.commit()
                auth_msg = AuthAssignedMessage(device_auth_token=device_auth_token)
                await websocket.send_json(auth_msg.model_dump(mode="json"))
                logger.info("Auth token assigned to existing device %s", device_id)

            # Update device stats
            device.firmware_version = raw.get("firmware_version", device.firmware_version)
            device.device_type = raw.get("device_type", device.device_type)
            device.storage_capacity_mb = raw.get("storage_capacity_mb", device.storage_capacity_mb)
            device.storage_used_mb = raw.get("storage_used_mb", device.storage_used_mb)
            device.last_seen = datetime.now(timezone.utc)
            await db.commit()

            # Auto-assign profile if not already set
            await _auto_assign_profile(device, db)

        # ── 3. Register connection ──
        client_ip = raw.get("ip_address") or (websocket.client.host if websocket.client else None)
        device_manager.register(device_id, websocket, ip_address=client_ip)

        # ── 4. Build base URL for asset downloads ──
        ws_url = websocket.url
        scheme = "https" if ws_url.scheme == "wss" else "http"
        base_url = f"{scheme}://{ws_url.hostname}"
        if ws_url.port and ws_url.port not in (80, 443):
            base_url += f":{ws_url.port}"

        # ── 5. Send full schedule sync ──
        sync = await build_device_sync(device_id, db)
        if sync:
            await websocket.send_json(sync.model_dump(mode="json"))

        # ── 6. If device is approved and has a default asset, push it ──
        await db.refresh(device, ["default_asset", "group"])
        default_asset = device.default_asset
        if not default_asset and device.group:
            await db.refresh(device.group, ["default_asset"])
            default_asset = device.group.default_asset

        if device.status == DeviceStatus.APPROVED and default_asset:
            fetch = await _resolve_asset_for_device(default_asset, device, base_url, db)
            if fetch:
                await websocket.send_json(fetch.model_dump(mode="json"))
                logger.info("Sent fetch_asset for default asset %s to %s", default_asset.filename, device_id)

        # ── 7. Push API key on connect (generate if missing) ──
        if device.status == DeviceStatus.APPROVED:
            await _generate_and_push_api_key(device, websocket, db)

        # ── 8. Message loop ──
        settings = get_settings()
        while True:
            msg = await websocket.receive_json()
            msg_type = msg.get("type")

            if msg_type == MessageType.STATUS:
                device.last_seen = datetime.now(timezone.utc)
                device.storage_used_mb = msg.get("storage_used_mb", device.storage_used_mb)
                await db.commit()

                # Track playback state
                device_manager.update_status(
                    device_id,
                    mode=msg.get("mode", "unknown"),
                    asset=msg.get("asset"),
                    uptime_seconds=msg.get("uptime_seconds", 0),
                    cpu_temp_c=msg.get("cpu_temp_c"),
                )

                # Rotate API key if due
                if (
                    device.status == DeviceStatus.APPROVED
                    and settings.api_key_rotation_hours > 0
                    and device.api_key_rotated_at
                ):
                    age = datetime.now(timezone.utc) - device.api_key_rotated_at
                    if age > timedelta(hours=settings.api_key_rotation_hours):
                        await _generate_and_push_api_key(device, websocket, db)
                        logger.info("API key rotated for device %s", device_id)

            elif msg_type == MessageType.ASSET_ACK:
                logger.info("Device %s confirmed asset: %s", device_id, msg.get("asset_name"))

            elif msg_type == MessageType.ASSET_DELETED:
                logger.info("Device %s deleted asset: %s", device_id, msg.get("asset_name"))

            elif msg_type == MessageType.FETCH_REQUEST:
                asset_name = msg.get("asset", "")
                if asset_name:
                    asset_result = await db.execute(
                        select(Asset).where(Asset.filename == asset_name)
                    )
                    asset = asset_result.scalar_one_or_none()
                    if asset:
                        await db.refresh(device)
                        fetch = await _resolve_asset_for_device(asset, device, base_url, db)
                        if fetch:
                            await websocket.send_json(fetch.model_dump(mode="json"))
                            logger.info("Device %s requested asset %s, sending fetch", device_id, asset_name)
                        else:
                            logger.info("Device %s requested asset %s, variant not ready yet", device_id, asset_name)
                    else:
                        logger.warning("Device %s requested unknown asset: %s", device_id, asset_name)

            elif msg_type == MessageType.FETCH_FAILED:
                logger.warning(
                    "Device %s failed to fetch asset %s: %s (budget=%sMB, available=%sMB, required=%sMB)",
                    device_id, msg.get("asset"), msg.get("reason"),
                    msg.get("budget_mb"), msg.get("available_mb"), msg.get("required_mb"),
                )

            else:
                logger.warning("Unknown message type from %s: %s", device_id, msg_type)

    except WebSocketDisconnect:
        logger.info("Device %s disconnected", device_id)
    except Exception as e:
        logger.error("WebSocket error for %s: %s", device_id, e)
    finally:
        if device_id:
            device_manager.disconnect(device_id)
