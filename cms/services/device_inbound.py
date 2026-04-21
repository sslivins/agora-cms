"""Per-message dispatch for inbound device messages.

Extracted from ``cms.routers.ws`` so the same logic can be reused by a
WebPubSub webhook receiver (issue #344 stage 2b.2). Behaviour must remain
identical to the WS path — all state mutations that the outer handler
depends on (``group_id``, ``device_name``, ``device_status``, ``group_name``,
and ``device`` row state) are mutated in-place on the shared
``InboundContext`` / ORM instance.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device import Device, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.schedule_log import ScheduleLogEvent
from cms.schemas.protocol import (
    ConfigMessage,
    FetchAssetMessage,
    MessageType,
)
from cms.services.alert_service import alert_service
from cms.services.device_manager import device_manager
from cms.services.scheduler import (
    clear_now_playing,
    log_schedule_event,
    set_now_playing,
)
from cms.services.storage import get_storage

logger = logging.getLogger("agora.cms.ws")


def _hash_token(token: str) -> str:
    """SHA-256 hash of a token for storage."""
    return hashlib.sha256(token.encode()).hexdigest()


@dataclass
class InboundContext:
    """Mutable per-connection state shared across message dispatches."""

    device_id: str
    device: Any  # SQLAlchemy Device instance — kept fresh via db.refresh inside handlers
    base_url: str
    settings: Any  # from cms.auth.get_settings()
    group_id: str | None = None
    device_name: str = ""
    device_status: str = "pending"
    group_name: str = ""


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


async def rotate_api_key(
    *,
    device: Device,
    db: AsyncSession,
    send: Callable[[dict], Awaitable[None]],
) -> None:
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
    await send(config_msg.model_dump(mode="json"))
    logger.info("API key rotated for device %s (previous key preserved)", device.id)


async def dispatch_device_message(
    *,
    msg: dict,
    ctx: InboundContext,
    db: AsyncSession,
    send: Callable[[dict], Awaitable[None]],
) -> None:
    """Handle a single inbound message from a device.

    ``send`` is how the handler replies to the device: it wraps
    ``websocket.send_json`` on the WS path and ``transport.send_to_device`` on
    the WPS path.
    """
    device = ctx.device
    device_id = ctx.device_id
    base_url = ctx.base_url
    settings = ctx.settings

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
        ctx.group_id = str(device.group_id) if device.group_id else None
        ctx.device_name = device.name or device_id
        ctx.device_status = device.status.value
        ctx.group_name = ""
        if device.group_id and device.group is not None:
            ctx.group_name = device.group.name or ""

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
                if ctx.group_id:
                    try:
                        _gid_uuid = uuid.UUID(ctx.group_id)
                    except (TypeError, ValueError):
                        _gid_uuid = None
                db.add(DeviceEvent(
                    device_id=device_id,
                    device_name=ctx.device_name,
                    group_id=_gid_uuid,
                    group_name=ctx.group_name,
                    event_type=_ev_type.value,
                ))
                await db.commit()
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
                if ctx.group_id:
                    try:
                        _gid_uuid = uuid.UUID(ctx.group_id)
                    except (TypeError, ValueError):
                        _gid_uuid = None
                _details = {"error": _new_error} if _emit_error else {"previous_error": _prev_error}
                db.add(DeviceEvent(
                    device_id=device_id,
                    device_name=ctx.device_name,
                    group_id=_gid_uuid,
                    group_name=ctx.group_name,
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
            device_name=ctx.device_name,
            group_id=ctx.group_id,
            group_name=ctx.group_name,
            status=ctx.device_status,
        )

        # Rotate API key if due
        if (
            device.status == DeviceStatus.ADOPTED
            and settings.api_key_rotation_hours > 0
            and device.api_key_rotated_at
        ):
            age = datetime.now(timezone.utc) - device.api_key_rotated_at
            if age > timedelta(hours=settings.api_key_rotation_hours):
                await rotate_api_key(device=device, db=db, send=send)

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
                    await send(fetch.model_dump(mode="json"))
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
