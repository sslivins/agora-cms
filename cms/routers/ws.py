"""WebSocket endpoint for device connections."""

import asyncio
import hashlib
import logging
import secrets
import uuid
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
from cms.models.schedule import Schedule
from cms.models.schedule_log import ScheduleLogEvent
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
from cms.services.alert_service import alert_service
from cms.services.scheduler import build_device_sync, log_schedule_event, set_now_playing, clear_now_playing
from cms.services.storage import get_storage

logger = logging.getLogger("agora.cms.ws")

router = APIRouter()

# Device sends status every 30s; timeout at 45s to detect dead connections quickly
WS_RECEIVE_TIMEOUT = 45


def _hash_token(token: str) -> str:
    """SHA-256 hash of a token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


def get_asset_base_url(request=None) -> str:
    """Return the base URL for asset download links.

    Priority:
    1. AGORA_CMS_ASSET_BASE_URL config override
    2. Request Host header (what the client actually connected to)
    3. Request base_url (server-side view — last resort)
    """
    settings = get_settings()
    if settings.asset_base_url:
        return settings.asset_base_url.rstrip("/")
    if request is not None:
        host = request.headers.get("host")
        if host:
            scheme = "https" if request.url.scheme in ("https", "wss") else "http"
            return f"{scheme}://{host}"
        return str(request.base_url).rstrip("/")
    return "http://localhost:8080"


async def _resolve_asset_for_device(
    asset: Asset, device: Device, base_url: str, db: AsyncSession,
) -> FetchAssetMessage | None:
    """Build a FetchAssetMessage using the best variant for the device's profile.

    For video and image assets with a profile:
      - If a READY variant exists → use variant download URL
      - If variant exists but not ready → return None (not available yet)
    For devices without a profile → use source asset directly.
    """
    storage = get_storage()

    # Saved streams behave like videos for download purposes
    is_file_asset = asset.asset_type in (AssetType.VIDEO, AssetType.IMAGE, AssetType.SAVED_STREAM)

    if is_file_asset and device.profile_id:
        # "Latest-READY-wins": with the variant-swap model multiple variant
        # rows may exist transiently for the same (asset, profile) pair.
        # Pick the most recently created READY non-deleted variant so devices
        # always get the freshest completed transcode.  If no READY variant
        # exists yet, we must still signal "not available" (rather than
        # falling through to the untranscoded source) when there IS a
        # non-terminal variant in flight.
        ready_result = await db.execute(
            select(AssetVariant)
            .where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == device.profile_id,
                AssetVariant.status == VariantStatus.READY,
                AssetVariant.deleted_at.is_(None),
            )
            .order_by(AssetVariant.created_at.desc())
            .limit(1)
        )
        variant = ready_result.scalars().first()
        if variant:
            api_url = f"{base_url}/api/assets/variants/{variant.id}/download"
            download_url = await storage.get_device_download_url(
                f"variants/{variant.filename}", api_url,
            )
            return FetchAssetMessage(
                asset_name=asset.filename,
                download_url=download_url,
                checksum=variant.checksum,
                size_bytes=variant.size_bytes,
                asset_type=asset.asset_type.value,
            )

        # No READY variant — check if any non-deleted variant is in flight.
        inflight = await db.execute(
            select(AssetVariant.id)
            .where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == device.profile_id,
                AssetVariant.deleted_at.is_(None),
            )
            .limit(1)
        )
        if inflight.scalar_one_or_none() is not None:
            # Variant exists but not ready — skip for now
            return None

    # No profile or no variant → use source
    api_url = f"{base_url}/api/assets/{asset.id}/download"
    download_url = await storage.get_device_download_url(asset.filename, api_url)
    return FetchAssetMessage(
        asset_name=asset.filename,
        download_url=download_url,
        checksum=asset.checksum,
        size_bytes=asset.size_bytes,
        asset_type=asset.asset_type.value,
    )


# Mapping of device_type substrings to built-in profile names.
# Checked in order — more specific patterns first.
_DEVICE_TYPE_PROFILE_MAP = {
    "pi zero 2 w": "pi-zero-2w",
    "raspberry pi zero 2 w": "pi-zero-2w",
    "pi 5": "pi-5",
    "pi 4": "pi-4",
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
    """Generate a new device API key, store its hash, and push it to the device.

    The previous key hash is preserved so in-flight downloads using the old
    key still succeed during the short window before the device starts using
    the new key.
    """
    new_key = secrets.token_urlsafe(32)
    device.previous_api_key_hash = device.device_api_key_hash
    device.device_api_key_hash = _hash_token(new_key)
    device.api_key_rotated_at = datetime.now(timezone.utc)
    await db.commit()
    config_msg = ConfigMessage(api_key=new_key)
    await websocket.send_json(config_msg.model_dump(mode="json"))
    logger.info("API key rotated for device %s (previous key preserved)", device.id)


@router.websocket("/ws/device")
async def device_websocket(websocket: WebSocket, db: AsyncSession = Depends(get_db)):
    await websocket.accept()
    device_id = None
    device = None
    _group_id = None
    _group_name = ""
    _device_name = ""
    _device_status = "pending"

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
            # New device — create as pending, prefer device_name over raw ID
            device_name = raw.get("device_name", "") or device_id
            device = Device(
                id=device_id,
                name=device_name,
                status=DeviceStatus.PENDING,
                firmware_version=raw.get("firmware_version", ""),
                device_type=raw.get("device_type", ""),
                supported_codecs=",".join(raw.get("supported_codecs", [])),
                storage_capacity_mb=raw.get("storage_capacity_mb", 0),
                storage_used_mb=raw.get("storage_used_mb", 0),
                last_seen=datetime.now(timezone.utc),
            )
            db.add(device)
            await db.commit()
            await db.refresh(device)
            logger.info("New device registered: %s (pending adoption)", device_id)

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
                if not auth_token:
                    # Empty token = device was re-flashed / factory reset.
                    # Reset to PENDING and assign a new auth token so the
                    # device can re-register without admin intervention.
                    logger.info(
                        "Device %s connected with empty token (likely re-flashed) "
                        "— resetting to pending", device_id,
                    )
                    device.status = DeviceStatus.PENDING
                    device.device_auth_token_hash = None
                    device.last_seen = datetime.now(timezone.utc)
                    await db.commit()

                    device_auth_token = secrets.token_urlsafe(32)
                    device.device_auth_token_hash = _hash_token(device_auth_token)
                    await db.commit()

                    auth_msg = AuthAssignedMessage(device_auth_token=device_auth_token)
                    await websocket.send_json(auth_msg.model_dump(mode="json"))
                    logger.info("New auth token assigned to re-flashed device %s", device_id)
                elif _hash_token(auth_token) != device.device_auth_token_hash:
                    logger.warning("Device %s failed auth — marking as orphaned", device_id)
                    device.status = DeviceStatus.ORPHANED
                    device.last_seen = datetime.now(timezone.utc)
                    await db.commit()
                    await websocket.send_json({"error": "Invalid credentials — device marked as orphaned. An admin must re-adopt this device."})
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
            reg_codecs = raw.get("supported_codecs")
            if reg_codecs is not None:
                device.supported_codecs = ",".join(reg_codecs)
            device.storage_capacity_mb = raw.get("storage_capacity_mb", device.storage_capacity_mb)
            device.storage_used_mb = raw.get("storage_used_mb", device.storage_used_mb)
            device.last_seen = datetime.now(timezone.utc)
            # Update name only if user explicitly set it via captive portal
            reg_name = raw.get("device_name", "")
            if reg_name and raw.get("device_name_custom", False):
                device.name = reg_name
            await db.commit()

            # Auto-assign profile if not already set
            await _auto_assign_profile(device, db)

        # ── 3. Register connection ──
        client_ip = raw.get("ip_address") or (websocket.client.host if websocket.client else None)
        device_manager.register(device_id, websocket, ip_address=client_ip)

        # ── 3b. Notify alert service of reconnection ──
        _group_id = str(device.group_id) if device.group_id else None
        _device_name = device.name or device_id
        _device_status = device.status.value
        _group_name = ""
        if device.group_id:
            from cms.models.device import DeviceGroup
            _grp_result = await db.execute(
                select(DeviceGroup.name).where(DeviceGroup.id == device.group_id)
            )
            _group_name = _grp_result.scalar_one_or_none() or ""
        alert_service.device_reconnected(
            device_id,
            device_name=_device_name,
            group_id=_group_id,
            group_name=_group_name,
            status=_device_status,
        )

        # ── 4. Build base URL for asset downloads ──
        base_url = get_asset_base_url(websocket)
        settings = get_settings()

        logger.info("Device %s: asset base_url = %s", device_id, base_url)

        # ── 5. Send full schedule sync (adopted devices only) ──
        if device.status == DeviceStatus.ADOPTED:
            sync = await build_device_sync(device_id, db)
            if sync:
                await websocket.send_json(sync.model_dump(mode="json"))
        else:
            logger.info("Device %s is %s — skipping sync until adopted", device_id, device.status.value)

        # ── 6. If device is adopted and has a default asset, push it ──
        await db.refresh(device, ["default_asset", "group"])
        default_asset = device.default_asset
        if not default_asset and device.group:
            await db.refresh(device.group, ["default_asset"])
            default_asset = device.group.default_asset

        if device.status == DeviceStatus.ADOPTED and default_asset:
            fetch = await _resolve_asset_for_device(default_asset, device, base_url, db)
            if fetch:
                await websocket.send_json(fetch.model_dump(mode="json"))
                logger.info("Sent fetch_asset for default asset %s to %s", default_asset.filename, device_id)

        # ── 7. Push API key on connect (generate if missing) ──
        if device.status == DeviceStatus.ADOPTED:
            await _generate_and_push_api_key(device, websocket, db)

        # ── 8. Message loop ──
        while True:
            try:
                msg = await asyncio.wait_for(websocket.receive_json(), timeout=WS_RECEIVE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.info("Device %s timed out (no message in %ds)", device_id, WS_RECEIVE_TIMEOUT)
                break
            msg_type = msg.get("type")

            if msg_type == MessageType.STATUS:
                device.last_seen = datetime.now(timezone.utc)
                device.storage_used_mb = msg.get("storage_used_mb", device.storage_used_mb)
                await db.commit()

                # Refresh device group/name/status so group reassignments or
                # status changes made through the API (since this WS was
                # established) are reflected in alerting. Without this, a
                # device reassigned to a group mid-connection keeps firing
                # with the stale (usually None) group_id cached at handshake
                # and alert_service silently drops the sample.
                await db.refresh(device, ["group_id", "group", "name", "status"])
                _group_id = str(device.group_id) if device.group_id else None
                _device_name = device.name or device_id
                _device_status = device.status.value
                _group_name = ""
                if device.group_id and device.group is not None:
                    _group_name = device.group.name or ""

                # Track playback state (including error)
                # Capture previous values for transition detection *before* update_status overwrites them.
                _prev_conn = device_manager.get(device_id)
                _prev_display = _prev_conn.display_connected if _prev_conn else None
                _prev_error = _prev_conn.error if _prev_conn else None
                _new_display = msg.get("display_connected")
                _new_error = msg.get("error")

                device_manager.update_status(
                    device_id,
                    mode=msg.get("mode", "unknown"),
                    asset=msg.get("asset"),
                    uptime_seconds=msg.get("uptime_seconds", 0),
                    cpu_temp_c=msg.get("cpu_temp_c"),
                    error=msg.get("error"),
                    pipeline_state=msg.get("pipeline_state", "NULL"),
                    started_at=msg.get("started_at"),
                    playback_position_ms=msg.get("playback_position_ms"),
                    ssh_enabled=msg.get("ssh_enabled"),
                    local_api_enabled=msg.get("local_api_enabled"),
                    display_connected=msg.get("display_connected"),
                )

                # Emit DISPLAY_CONNECTED / DISPLAY_DISCONNECTED transitions.
                # Only fire on explicit True<->False flips. A None->bool
                # transition is treated as an initial observation and is
                # ignored to avoid noise on every reconnect.
                try:
                    if (
                        _new_display is not None
                        and _prev_display is not None
                        and bool(_new_display) != bool(_prev_display)
                    ):
                        from cms.models.device_event import DeviceEvent, DeviceEventType
                        _ev_type = (
                            DeviceEventType.DISPLAY_CONNECTED
                            if _new_display
                            else DeviceEventType.DISPLAY_DISCONNECTED
                        )
                        _gid_uuid = None
                        if _group_id:
                            try:
                                _gid_uuid = uuid.UUID(_group_id)
                            except (TypeError, ValueError):
                                _gid_uuid = None
                        db.add(DeviceEvent(
                            device_id=device_id,
                            device_name=_device_name,
                            group_id=_gid_uuid,
                            group_name=_group_name,
                            event_type=_ev_type.value,
                        ))
                        await db.commit()

                        # Notify alert service so it can manage the bell-notification
                        # grace period (event log entry written above is unconditional).
                        alert_service.display_state_changed(
                            device_id,
                            device_name=_device_name,
                            group_id=_group_id,
                            group_name=_group_name,
                            status=_device_status,
                            connected=bool(_new_display),
                        )
                except Exception:
                    logger.exception("Failed to log display transition for %s", device_id)

                # Emit ERROR / ERROR_CLEARED transitions. ERROR fires on a
                # None->str transition *or* when the error string changes.
                # ERROR_CLEARED fires on str->None.
                try:
                    _had_err = bool(_prev_error)
                    _has_err = bool(_new_error)
                    _emit_error = False
                    _emit_cleared = False
                    if _has_err and not _had_err:
                        _emit_error = True
                    elif _has_err and _had_err and _new_error != _prev_error:
                        _emit_error = True
                    elif _had_err and not _has_err:
                        _emit_cleared = True

                    if _emit_error or _emit_cleared:
                        from cms.models.device_event import DeviceEvent, DeviceEventType
                        _ev_type = (
                            DeviceEventType.ERROR
                            if _emit_error
                            else DeviceEventType.ERROR_CLEARED
                        )
                        _gid_uuid = None
                        if _group_id:
                            try:
                                _gid_uuid = uuid.UUID(_group_id)
                            except (TypeError, ValueError):
                                _gid_uuid = None
                        _details = {"error": _new_error} if _emit_error else {"previous_error": _prev_error}
                        db.add(DeviceEvent(
                            device_id=device_id,
                            device_name=_device_name,
                            group_id=_gid_uuid,
                            group_name=_group_name,
                            event_type=_ev_type.value,
                            details=_details,
                        ))
                        await db.commit()
                except Exception:
                    logger.exception("Failed to log error transition for %s", device_id)

                # Check temperature thresholds
                alert_service.check_temperature(
                    device_id,
                    cpu_temp_c=msg.get("cpu_temp_c"),
                    device_name=_device_name,
                    group_id=_group_id,
                    group_name=_group_name,
                    status=_device_status,
                )

                # Rotate API key if due
                if (
                    device.status == DeviceStatus.ADOPTED
                    and settings.api_key_rotation_hours > 0
                    and device.api_key_rotated_at
                ):
                    age = datetime.now(timezone.utc) - device.api_key_rotated_at
                    if age > timedelta(hours=settings.api_key_rotation_hours):
                        await _generate_and_push_api_key(device, websocket, db)

            elif msg_type == MessageType.ASSET_ACK:
                logger.info("Device %s confirmed asset: %s", device_id, msg.get("asset_name"))

            elif msg_type == MessageType.ASSET_DELETED:
                logger.info("Device %s deleted asset: %s", device_id, msg.get("asset_name"))

            elif msg_type == MessageType.WIPE_ASSETS_ACK:
                logger.info("Device %s confirmed asset wipe (reason: %s)", device_id, msg.get("reason", ""))

            elif msg_type == MessageType.FETCH_REQUEST:
                asset_name = msg.get("asset", "")
                if asset_name:
                    asset_result = await db.execute(
                        select(Asset).where(
                            Asset.filename == asset_name,
                            Asset.deleted_at.is_(None),
                        )
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

            elif msg_type == MessageType.PLAYBACK_STARTED:
                schedule_id = msg.get("schedule_id", "")
                schedule_name = msg.get("schedule_name", "")
                asset_name = msg.get("asset", "")
                device_ts = msg.get("timestamp", "")

                # Look up the schedule (with asset) for display name resolution
                sched_result = await db.execute(
                    select(Schedule)
                    .options(selectinload(Schedule.asset))
                    .where(Schedule.id == schedule_id)
                )
                sched = sched_result.scalar_one_or_none()

                # For webpage/stream assets the device sends the raw URL as
                # ``asset``; prefer the human-readable display name
                # stored in the DB (original_filename / filename).
                display_name = asset_name
                if sched and sched.asset:
                    display_name = (
                        sched.asset.display_name
                        or sched.asset.original_filename
                        or sched.asset.filename
                        or asset_name
                    )

                device_name = device.name or device_id

                # Store minimal confirmation — the dashboard computes
                # the rest from the DB via compute_now_playing().
                set_now_playing(device_id, {
                    "schedule_id": schedule_id,
                    "since": device_ts or datetime.now(timezone.utc).isoformat(),
                })

                await log_schedule_event(
                    db, ScheduleLogEvent.STARTED,
                    schedule_name=schedule_name,
                    device_name=device_name,
                    asset_filename=display_name,
                    schedule_id=schedule_id,
                    device_id=device_id,
                )
                await db.commit()
                logger.info(
                    "Device %s started playing %s (schedule %s)",
                    device_id, asset_name, schedule_name,
                )

            elif msg_type == MessageType.PLAYBACK_ENDED:
                schedule_id = msg.get("schedule_id", "")
                schedule_name = msg.get("schedule_name", "")
                asset_name = msg.get("asset", "")
                device_name = device.name or device_id

                # Resolve display name for webpage assets
                ended_display_name = asset_name
                ended_sched_result = await db.execute(
                    select(Schedule)
                    .options(selectinload(Schedule.asset))
                    .where(Schedule.id == schedule_id)
                )
                ended_sched = ended_sched_result.scalar_one_or_none()
                if ended_sched and ended_sched.asset:
                    ended_display_name = (
                        ended_sched.asset.display_name
                        or ended_sched.asset.original_filename
                        or ended_sched.asset.filename
                        or asset_name
                    )

                clear_now_playing(device_id)

                await log_schedule_event(
                    db, ScheduleLogEvent.ENDED,
                    schedule_name=schedule_name,
                    device_name=device_name,
                    asset_filename=ended_display_name,
                    schedule_id=schedule_id,
                    device_id=device_id,
                )
                await db.commit()
                logger.info(
                    "Device %s ended playing %s (schedule %s)",
                    device_id, asset_name, schedule_name,
                )

            elif msg_type == MessageType.LOGS_RESPONSE:
                request_id = msg.get("request_id", "")
                logs = msg.get("logs", {})
                error = msg.get("error")
                logger.info("Device %s sent logs (request %s, %d services)", device_id, request_id, len(logs))
                device_manager.resolve_log_request(request_id, logs, error)

            else:
                logger.warning("Unknown message type from %s: %s", device_id, msg_type)

    except WebSocketDisconnect:
        logger.info("Device %s disconnected", device_id)
    except Exception as e:
        logger.error("WebSocket error for %s: %s", device_id, e)
    finally:
        if device_id:
            device_manager.disconnect(device_id)
            # Refresh group/name/status so a group reassignment made mid-connection
            # is reflected in the disconnect notification. The STATUS path already
            # refreshes these (see above), but a device may disconnect before any
            # STATUS heartbeat arrives — in which case the cached values from the
            # handshake are stale and the offline alert routes to the wrong group
            # (or silently drops when the device was originally ungrouped).
            try:
                if device is not None:
                    await db.refresh(device, ["group_id", "group", "name", "status"])
                    _group_id = str(device.group_id) if device.group_id else None
                    _device_name = device.name or device_id
                    _device_status = device.status.value
                    _group_name = (device.group.name if device.group else "") or ""
            except Exception as refresh_err:
                logger.warning(
                    "Failed to refresh device %s before disconnect notification: %s",
                    device_id, refresh_err,
                )
            # Notify alert service of disconnection
            alert_service.device_disconnected(
                device_id,
                device_name=_device_name or device_id,
                group_id=_group_id,
                group_name=_group_name,
                status=_device_status,
            )
            # Clear upgrade-in-progress flag so the device can be upgraded again after reconnect
            from cms.routers.devices import _upgrading
            _upgrading.discard(device_id)
