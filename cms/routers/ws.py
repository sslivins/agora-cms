"""WebSocket endpoint for device connections."""

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, WebSocket, WebSocketDisconnect
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_settings
from cms.database import get_db
from cms.models.device import Device, DeviceStatus
from cms.schemas.protocol import (
    PROTOCOL_VERSION,
    SUPPORTED_PROTOCOL_VERSIONS,
    AuthAssignedMessage,
    MessageType,
)
from cms.services.device_inbound import (
    InboundContext,
    _resolve_asset_for_device,
    dispatch_device_message,
    rotate_api_key,
)
from cms.services.device_manager import device_manager
from cms.services.device_register import (
    _DEVICE_TYPE_PROFILE_MAP,
    auto_assign_profile as _auto_assign_profile,
    hash_token as _hash_token,
    register_known_device,
)
from cms.services.alert_service import alert_service
from cms.services.log_chunk_assembler import (
    handle_frame as handle_log_chunk_frame,
    is_chunk_frame,
)
from cms.services.scheduler import build_device_sync

logger = logging.getLogger("agora.cms.ws")

router = APIRouter()

# Device sends status every 30s; timeout at 45s to detect dead connections quickly
WS_RECEIVE_TIMEOUT = 45


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


# Mapping of device_type substrings to built-in profile names moved to
# cms/services/device_register.py so the WPS webhook path can share it.


@router.websocket("/ws/device")
async def device_websocket(websocket: WebSocket, db: AsyncSession = Depends(get_db)):
    await websocket.accept()
    device_id = None
    device = None
    ctx: InboundContext | None = None
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
        if protocol not in SUPPORTED_PROTOCOL_VERSIONS:
            await websocket.send_json({
                "error": (
                    f"Protocol mismatch: supported {sorted(SUPPORTED_PROTOCOL_VERSIONS)}, "
                    f"got {protocol}"
                )
            })
            await websocket.close(code=4003)
            return

        # ── 2. Authenticate ──
        result = await db.execute(select(Device).where(Device.id == device_id))
        device = result.scalar_one_or_none()
        is_new_device = device is None
        pre_register_fw: str = ""
        pre_register_upgrade_claim = None  # datetime or None

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
            # Known device — delegate to the shared helper that the WPS
            # webhook path also uses.  Orphaned → close 4004; otherwise
            # push any newly-minted auth token back to the device.
            # Capture pre-register firmware AND upgrade-claim token
            # before the helper mutates them.  Firmware-change gates
            # whether we clear the claim; the claim token itself is
            # used as a compare-and-swap guard so a successor upgrade
            # claim (written between our SELECT and our CLEAR) isn't
            # wiped by this register path.
            pre_register_fw = device.firmware_version or ""
            pre_register_upgrade_claim = device.upgrade_started_at
            reg_result = await register_known_device(device, raw, db)
            if reg_result.orphaned:
                await websocket.send_json({
                    "error": (
                        "Invalid credentials — device marked as orphaned. "
                        "An admin must re-adopt this device."
                    )
                })
                await websocket.close(code=4004)
                return
            if reg_result.auth_assigned is not None:
                await websocket.send_json(reg_result.auth_assigned)

        # ── 3. Register connection ──
        client_ip = raw.get("ip_address") or (websocket.client.host if websocket.client else None)
        device_manager.register(device_id, websocket, ip_address=client_ip)
        # Stage 2c: reflect presence in the DB so every replica can see it.
        # Stage 4: generate a per-connection UUID token and persist it on
        # the Device row.  The disconnect handler below compares this
        # token against the current column value before marking offline,
        # so a stale socket closing on replica A after the device has
        # already reconnected on replica B can't flip the fresh
        # connection offline.
        from cms.services import device_presence
        conn_id = str(uuid.uuid4())
        await device_presence.mark_online(
            db, device_id, connection_id=conn_id, ip_address=client_ip,
        )
        # Stage 4: clear any stale upgrade-in-progress claim so the
        # Stage 4: clear any stale upgrade-in-progress claim so the
        # device can be upgraded again on a future request — but only
        # when this register represents an actual completed upgrade:
        #   - there was a claim at register time (``pre_register_upgrade_claim``),
        #   - both the prior and reported firmware are non-empty, and
        #   - they differ (so the device booted into a new version).
        # The UPDATE uses compare-and-swap on ``upgrade_started_at``
        # equal to the pre-register value, so a successor upgrade
        # claim written between our SELECT and our clear isn't wiped.
        # Under N>1 with rolling restarts, transient reconnects during
        # an upgrade (same firmware) leave the claim in place; the
        # TTL on the column (see upgrade endpoint) is the safety net
        # that releases the claim if no firmware change is ever
        # reported.  For brand-new devices there's no claim to clear.
        if not is_new_device and pre_register_upgrade_claim is not None:
            reported_fw = (raw.get("firmware_version") or "").strip()
            prior_fw = (pre_register_fw or "").strip()
            if reported_fw and prior_fw and reported_fw != prior_fw:
                await db.execute(
                    update(Device)
                    .where(
                        Device.id == device_id,
                        Device.upgrade_started_at == pre_register_upgrade_claim,
                    )
                    .values(upgrade_started_at=None)
                    .execution_options(synchronize_session=False)
                )
                await db.commit()

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

        # Build dispatch context and WS send adapter. Both the handshake
        # helpers below and the per-message dispatch share this ctx so state
        # mutations inside dispatch are visible to the outer `finally` block.
        ctx = InboundContext(
            device_id=device_id,
            device=device,
            base_url=base_url,
            settings=settings,
            group_id=_group_id,
            device_name=_device_name,
            device_status=_device_status,
            group_name=_group_name,
        )

        async def _ws_send(message: dict) -> None:
            await websocket.send_json(message)

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
            await rotate_api_key(device=device, db=db, send=_ws_send)

        # ── 8. Message loop ──
        while True:
            try:
                event = await asyncio.wait_for(websocket.receive(), timeout=WS_RECEIVE_TIMEOUT)
            except asyncio.TimeoutError:
                logger.info("Device %s timed out (no message in %ds)", device_id, WS_RECEIVE_TIMEOUT)
                break

            # Starlette surfaces disconnects as dicts with type=websocket.disconnect.
            event_type = event.get("type")
            if event_type == "websocket.disconnect":
                raise WebSocketDisconnect(code=event.get("code", 1005))
            if event_type != "websocket.receive":
                logger.warning("Device %s sent unknown event type %s", device_id, event_type)
                continue

            # Stage 3c (#345): binary frames tagged with the LGCK magic
            # carry chunked LOGS_RESPONSE payloads too large for a single
            # WPS message (~1 MiB cap).  Dispatch them to the assembler
            # and skip JSON parsing.
            binary = event.get("bytes")
            if binary is not None:
                if is_chunk_frame(binary):
                    try:
                        await handle_log_chunk_frame(
                            db, device_id=device_id, frame_bytes=binary,
                        )
                    except Exception:
                        logger.exception(
                            "Log chunk handling failed for device %s", device_id,
                        )
                    continue
                logger.warning(
                    "Device %s sent unknown binary frame (%d bytes)",
                    device_id, len(binary),
                )
                continue

            text = event.get("text")
            if text is None:
                continue
            import json
            try:
                msg = json.loads(text)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "Device %s sent malformed JSON: %s", device_id, exc,
                )
                continue
            await dispatch_device_message(msg=msg, ctx=ctx, db=db, send=_ws_send)

    except WebSocketDisconnect:
        logger.info("Device %s disconnected", device_id)
    except Exception as e:
        logger.error("WebSocket error for %s: %s", device_id, e)
    finally:
        if device_id:
            device_manager.disconnect(device_id)
            # Stage 2c/4: clear the DB presence flag so other replicas
            # stop treating this device as online.  The Stage 4
            # connection-id guard ensures a stale disconnect on replica
            # A doesn't flip a fresh connection on replica B offline.
            was_current = False
            try:
                from cms.services import device_presence
                was_current = await device_presence.mark_offline(
                    db, device_id,
                    expected_connection_id=(conn_id if "conn_id" in locals() else None),
                )
            except Exception:
                logger.exception("Failed to mark %s offline in DB", device_id)
            # If the disconnect is stale (connection_id has already been
            # replaced by a newer register on another replica), skip the
            # alert-service disconnect path entirely — the device is not
            # actually offline.
            if not was_current and "conn_id" in locals():
                logger.info(
                    "Device %s: stale disconnect suppressed "
                    "(connection_id was replaced)",
                    device_id,
                )
                return
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
                elif ctx is not None:
                    _group_id = ctx.group_id
                    _device_name = ctx.device_name
                    _device_status = ctx.device_status
                    _group_name = ctx.group_name
            except Exception as refresh_err:
                logger.warning(
                    "Failed to refresh device %s before disconnect notification: %s",
                    device_id, refresh_err,
                )
                if ctx is not None:
                    _group_id = ctx.group_id
                    _device_name = ctx.device_name
                    _device_status = ctx.device_status
                    _group_name = ctx.group_name
            # Notify alert service of disconnection
            alert_service.device_disconnected(
                device_id,
                device_name=_device_name or device_id,
                group_id=_group_id,
                group_name=_group_name,
                status=_device_status,
            )
            # Stage 4: ``_upgrading`` set has been replaced with the
            # ``devices.upgrade_started_at`` column; the register path
            # clears it on reconnect.  No explicit discard needed here.
