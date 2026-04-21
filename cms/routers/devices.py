"""Device management API routes."""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import get_settings, get_user_group_ids, require_auth, require_permission, verify_resource_group_access
from cms.database import get_db
from cms.permissions import (
    DEVICES_READ, DEVICES_WRITE, DEVICES_MANAGE,
    GROUPS_READ, GROUPS_WRITE,
)
from cms.models.asset import Asset
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_profile import DeviceProfile
from cms.schemas.device import (
    AdoptRequest,
    DeviceGroupCreate,
    DeviceGroupOut,
    DeviceGroupUpdate,
    DeviceOut,
    DeviceUpdate,
    LogRequest,
    SetPasswordRequest,
    ToggleRequest,
)
from cms.schemas.protocol import ConfigMessage, FactoryResetMessage, RebootMessage, SyncMessage, UpgradeMessage, WipeAssetsMessage
from cms.services.transport import get_transport
from cms.services.scheduler import push_sync_to_device
from cms.services.audit_service import audit_log, compute_diff
from cms.services.asset_readiness import require_asset_ready
from cms.services.version_checker import check_now

router = APIRouter(prefix="/api/devices", dependencies=[Depends(require_auth)])

# Separate router for device-originated endpoints — these authenticate
# via X-Device-API-Key, so they must NOT inherit the browser-session
# `require_auth` dependency from the main devices router.
device_originated_router = APIRouter(prefix="/api/devices", tags=["devices (device-originated)"])

# Track devices with an in-flight upgrade to prevent concurrent upgrade commands
_upgrading: set[str] = set()

# Device model columns that are *also* passed explicitly as kwargs when
# building a ``DeviceOut`` from the live-state + ORM row merge below.
# We exclude them from the ``**{c.key: getattr(d, c.key) ...}`` splat so
# Pydantic doesn't raise ``got multiple values for keyword argument``.
# These are the Stage 2c telemetry columns — they live on the Device row
# now, but the construction paths below still override them with the
# live_states dict produced from ``get_transport().get_all_states()``
# (which itself reads from the same DB row in Stage 2c; Stage 4 will
# collapse this into a single read).
_DEVICE_OUT_OVERLAP_COLUMNS = {
    "online",
    "connection_id",
    "last_status_ts",
    "cpu_temp_c",
    "load_avg",
    "uptime_seconds",
    "mode",
    "asset",
    "pipeline_state",
    "playback_started_at",
    "playback_position_ms",
    "error",
    "error_since",
    "ssh_enabled",
    "local_api_enabled",
    "display_connected",
}


def _device_row_kwargs(device: Device) -> dict:
    """Return ``DeviceOut`` kwargs drawn from the ORM row.

    Excludes columns that are supplied as explicit kwargs (live state,
    presence) at every ``DeviceOut(...)`` call site.
    """
    return {
        c.key: getattr(device, c.key)
        for c in Device.__table__.columns
        if c.key not in _DEVICE_OUT_OVERLAP_COLUMNS
    }


async def _get_device_with_access(
    device_id: str, request: Request, db: AsyncSession,
) -> Device:
    """Fetch a device by ID and verify the current user has group access."""
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    user = getattr(request.state, "user", None)
    if user:
        await verify_resource_group_access(user, db, device.group_id)
    return device


async def _push_default_asset(device_id: str, asset: Asset, base_url: str, db: AsyncSession) -> None:
    """Send fetch_asset for a default asset, then push a full sync.

    The sync includes the updated default_asset and splash fields, so the
    device evaluator will start playing it once downloaded.  We do NOT send
    a separate play command — that caused a race where the player tried to
    play an asset that hadn't finished downloading yet.
    """
    from cms.routers.ws import _resolve_asset_for_device

    device_q = await db.execute(select(Device).where(Device.id == device_id))
    device = device_q.scalar_one_or_none()
    if not device:
        return

    fetch = await _resolve_asset_for_device(asset, device, base_url, db)
    if fetch:
        await get_transport().send_to_device(device_id, fetch.model_dump(mode="json"))

    # Push a fresh sync so the device learns the new default_asset and splash
    # immediately (instead of waiting up to 15s for the scheduler cycle).
    await push_sync_to_device(device_id, db)


# ── Devices ──


@router.post("/check-updates", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def check_for_updates():
    """Trigger an immediate check for the latest device firmware version."""
    latest = await check_now()
    return {"latest_version": latest}


@router.get("", response_model=List[DeviceOut], dependencies=[Depends(require_permission(DEVICES_READ))])
async def list_devices(request: Request, db: AsyncSession = Depends(get_db)):
    from cms.services.scheduler import compute_now_playing
    from cms.auth import SETTING_TIMEZONE, get_setting
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone

    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)
    now = datetime.now(timezone.utc)

    user = getattr(request.state, "user", None)
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None

    # Hide pending/orphaned devices from users without devices:manage
    from cms.permissions import has_permission
    user_perms = user.role.permissions if user and user.role else []
    can_manage = has_permission(user_perms, DEVICES_MANAGE)

    query = select(Device).options(selectinload(Device.group)).order_by(Device.registered_at)
    if not can_manage:
        query = query.where(Device.status == DeviceStatus.ADOPTED)
    if not is_admin:
        # Non-admin: only devices in user's groups
        if group_ids:
            query = query.where(Device.group_id.in_(group_ids))
        else:
            query = query.where(False)

    result = await db.execute(query)
    devices = result.scalars().all()
    live_states = {s["device_id"]: s for s in get_transport().get_all_states()}
    scheduled_device_ids = {np["device_id"] for np in await compute_now_playing(db, tz, now)}

    # Build URL→display name map for resolving URL-based asset names
    from shared.models.asset import Asset as AssetModel
    url_assets_q = await db.execute(
        select(AssetModel.url, AssetModel.filename).where(AssetModel.url.isnot(None))
    )
    _url_display = {}
    for url, fname in url_assets_q.all():
        _url_display.setdefault(url, fname)

    def _resolve_asset_name(device_id: str) -> str | None:
        if device_id not in live_states:
            return None
        raw = live_states[device_id]["asset"]
        return _url_display.get(raw, raw) if raw else raw

    from cms.services.version_checker import is_update_available
    return [
        DeviceOut(
            **_device_row_kwargs(d),
            group_name=d.group.name if d.group else None,
            is_online=get_transport().is_connected(d.id),
            is_upgrading=d.id in _upgrading,
            playback_mode=live_states[d.id]["mode"] if d.id in live_states else None,
            playback_asset=_resolve_asset_name(d.id),
            pipeline_state=live_states[d.id]["pipeline_state"] if d.id in live_states else None,
            display_connected=live_states[d.id]["display_connected"] if d.id in live_states else None,
            cpu_temp_c=live_states[d.id]["cpu_temp_c"] if d.id in live_states else None,
            ip_address=live_states[d.id]["ip_address"] if d.id in live_states else None,
            ssh_enabled=live_states[d.id]["ssh_enabled"] if d.id in live_states else None,
            local_api_enabled=live_states[d.id]["local_api_enabled"] if d.id in live_states else None,
            error=live_states[d.id]["error"] if d.id in live_states else None,
            uptime_seconds=live_states[d.id]["uptime_seconds"] if d.id in live_states else 0,
            update_available=is_update_available(d.firmware_version),
            has_active_schedule=d.id in scheduled_device_ids,
        )
        for d in devices
    ]


@router.get("/{device_id}", response_model=DeviceOut, dependencies=[Depends(require_permission(DEVICES_READ))])
async def get_device(device_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    from cms.services.scheduler import compute_now_playing
    from cms.auth import SETTING_TIMEZONE
    from cms.ui import get_setting
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone

    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)
    now = datetime.now(timezone.utc)

    device = await _get_device_with_access(device_id, request, db)
    await db.refresh(device, ["group"])
    live_states = {s["device_id"]: s for s in get_transport().get_all_states()}
    scheduled_device_ids = {np["device_id"] for np in await compute_now_playing(db, tz, now)}

    # Resolve URL-based asset names
    raw_asset = live_states[device.id]["asset"] if device.id in live_states else None
    if raw_asset:
        from shared.models.asset import Asset as AssetModel
        url_q = await db.execute(
            select(AssetModel.filename).where(AssetModel.url == raw_asset).limit(1)
        )
        resolved = url_q.scalar_one_or_none()
        if resolved:
            raw_asset = resolved

    from cms.services.version_checker import is_update_available
    return DeviceOut(
        **_device_row_kwargs(device),
        group_name=device.group.name if device.group else None,
        is_online=get_transport().is_connected(device.id),
        is_upgrading=device.id in _upgrading,
        playback_mode=live_states[device.id]["mode"] if device.id in live_states else None,
        playback_asset=raw_asset,
        pipeline_state=live_states[device.id]["pipeline_state"] if device.id in live_states else None,
        display_connected=live_states[device.id]["display_connected"] if device.id in live_states else None,
        cpu_temp_c=live_states[device.id]["cpu_temp_c"] if device.id in live_states else None,
        ip_address=live_states[device.id]["ip_address"] if device.id in live_states else None,
        ssh_enabled=live_states[device.id]["ssh_enabled"] if device.id in live_states else None,
        local_api_enabled=live_states[device.id]["local_api_enabled"] if device.id in live_states else None,
        error=live_states[device.id]["error"] if device.id in live_states else None,
        uptime_seconds=live_states[device.id]["uptime_seconds"] if device.id in live_states else 0,
        update_available=is_update_available(device.firmware_version),
        has_active_schedule=device.id in scheduled_device_ids,
    )


@router.patch("/{device_id}", response_model=DeviceOut, dependencies=[Depends(require_permission(DEVICES_WRITE))])
async def update_device(
    device_id: str,
    data: DeviceUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from cms.permissions import has_permission

    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    user = getattr(request.state, "user", None)
    if user:
        await verify_resource_group_access(user, db, device.group_id)

    updates = data.model_dump(exclude_unset=True)

    # Fields that require devices:manage (admin-only)
    managed_fields = {"profile_id", "timezone", "status"}
    restricted = managed_fields & updates.keys()
    if restricted:
        perms = user.role.permissions if user and user.role else []
        if not has_permission(perms, DEVICES_MANAGE):
            raise HTTPException(
                status_code=403,
                detail=f"Missing permission: {DEVICES_MANAGE}",
            )

    # Gate splash assignment on variant readiness (issue #201).
    if updates.get("default_asset_id"):
        await require_asset_ready(db, updates["default_asset_id"])

    # Snapshot before mutation so we can build a true diff for the audit log
    changes = compute_diff(device, updates)

    for field, value in updates.items():
        setattr(device, field, value)
    await audit_log(
        db, user=user, action="device.update", resource_type="device",
        resource_id=str(device.id),
        description=f"Modified device '{device.name or device.id}'",
        details={"changes": changes},
        request=request,
    )
    await db.commit()
    await db.refresh(device, ["group", "default_asset"])

    # If default_asset_id was changed, resolve effective default and push
    if "default_asset_id" in updates:
        from cms.routers.ws import get_asset_base_url
        base_url = get_asset_base_url(request)
        # Resolve: device default → group default → none (splash)
        effective_asset = device.default_asset
        if not effective_asset and device.group:
            await db.refresh(device.group, ["default_asset"])
            effective_asset = device.group.default_asset

        if effective_asset:
            await _push_default_asset(device_id, effective_asset, base_url, db)
        else:
            # No default at any level — push a full sync so the device
            # clears its default_asset and shows splash correctly.
            await push_sync_to_device(device_id, db)

    # If timezone was changed, push a fresh sync so the device applies it
    elif "timezone" in updates:
        await push_sync_to_device(device_id, db)

    return DeviceOut(
        **_device_row_kwargs(device),
        group_name=device.group.name if device.group else None,
        is_online=get_transport().is_connected(device.id),
    )


@router.post("/{device_id}/password", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def set_device_password(
    device_id: str,
    body: SetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    password = body.password.strip()
    if not password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    device = await _get_device_with_access(device_id, request, db)

    if not get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    config_msg = ConfigMessage(web_password=password)
    sent = await get_transport().send_to_device(device_id, config_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.set_password", resource_type="device",
        resource_id=str(device_id),
        description=f"Reset web password on device '{device.name or device_id}'",
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/reboot", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def reboot_device(
    device_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_with_access(device_id, request, db)

    if not get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    reboot_msg = RebootMessage()
    sent = await get_transport().send_to_device(device_id, reboot_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.reboot", resource_type="device",
        resource_id=str(device_id),
        description=f"Rebooted device '{device.name or device_id}'",
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/upgrade", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def upgrade_device(
    device_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_with_access(device_id, request, db)

    if not get_transport().is_connected(device_id):
        _upgrading.discard(device_id)
        raise HTTPException(status_code=409, detail="Device is not connected")

    if device_id in _upgrading:
        raise HTTPException(status_code=409, detail="Upgrade already in progress for this device")

    _upgrading.add(device_id)
    upgrade_msg = UpgradeMessage()
    sent = await get_transport().send_to_device(device_id, upgrade_msg.model_dump(mode="json"))
    if not sent:
        _upgrading.discard(device_id)
        raise HTTPException(status_code=502, detail="Failed to send to device")

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.upgrade", resource_type="device",
        resource_id=str(device_id),
        description=f"Triggered firmware upgrade on device '{device.name or device_id}'",
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/ssh", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def toggle_device_ssh(
    device_id: str,
    body: ToggleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    enabled = body.enabled
    device = await _get_device_with_access(device_id, request, db)

    if not get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    config_msg = ConfigMessage(ssh_enabled=enabled)
    sent = await get_transport().send_to_device(device_id, config_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    # Track the SSH state immediately so the UI reflects it
    get_transport().set_state_flags(device_id, ssh_enabled=enabled)

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.ssh_toggle", resource_type="device",
        resource_id=str(device_id),
        description=f"{'Enabled' if enabled else 'Disabled'} SSH on device '{device.name or device_id}'",
        details={"enabled": enabled},
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/factory-reset", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def factory_reset_device(
    device_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_with_access(device_id, request, db)

    if not get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    msg = FactoryResetMessage()
    sent = await get_transport().send_to_device(device_id, msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.factory_reset", resource_type="device",
        resource_id=str(device_id),
        description=f"Triggered factory reset on device '{device.name or device_id}'",
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/local-api", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def toggle_device_local_api(
    device_id: str,
    body: ToggleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    enabled = body.enabled
    device = await _get_device_with_access(device_id, request, db)

    if not get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    config_msg = ConfigMessage(local_api_enabled=enabled)
    sent = await get_transport().send_to_device(device_id, config_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    get_transport().set_state_flags(device_id, local_api_enabled=enabled)

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.local_api_toggle", resource_type="device",
        resource_id=str(device_id),
        description=f"{'Enabled' if enabled else 'Disabled'} local API on device '{device.name or device_id}'",
        details={"enabled": enabled},
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/adopt", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def adopt_device(device_id: str, body: AdoptRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Adopt a pending device or re-adopt an orphaned one.

    For pending devices: sets status to adopted and assigns an auth token on next connect.
    For orphaned devices: clears stored auth credentials so a new token is assigned on reconnect.

    Optionally accepts a JSON body with name, location, and group_id to configure
    the device during adoption.

    In both cases, a wipe_assets command is sent so the device starts fresh
    without stale content from a previous adoption.

    Accepts optional name and group_id to configure the device during adoption.
    """
    device = await _get_device_with_access(device_id, request, db)

    if device.status == DeviceStatus.PENDING:
        device.status = DeviceStatus.ADOPTED
    elif device.status == DeviceStatus.ORPHANED:
        device.device_auth_token_hash = None
        device.device_api_key_hash = None
        device.previous_api_key_hash = None
        device.api_key_rotated_at = None
        device.status = DeviceStatus.ADOPTED
    else:
        raise HTTPException(status_code=400, detail="Device is already adopted")

    # Apply optional name, location, and group assignment
    if body.name is not None:
        device.name = body.name
    if body.location is not None:
        device.location = body.location
    if body.group_id is not None:
        # Verify the group exists
        grp = await db.execute(select(DeviceGroup).where(DeviceGroup.id == body.group_id))
        if not grp.scalar_one_or_none():
            raise HTTPException(status_code=404, detail="Group not found")
        device.group_id = body.group_id

    # Verify and assign the encoder profile (required)
    prof = await db.execute(select(DeviceProfile).where(DeviceProfile.id == body.profile_id))
    if not prof.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Profile not found")
    device.profile_id = body.profile_id

    await db.commit()

    # Tell the device to wipe cached assets so it starts clean
    wipe_msg = WipeAssetsMessage(reason="adopted")
    await get_transport().send_to_device(device_id, wipe_msg.model_dump(mode="json"))

    # Push a fresh sync so the device learns its new status immediately
    # (e.g. the OOBE screen advances from "waiting for adoption" to "adopted").
    await push_sync_to_device(device_id, db)

    # Resolve group name for the audit description
    group_name = None
    if device.group_id:
        grp_q = await db.execute(select(DeviceGroup.name).where(DeviceGroup.id == device.group_id))
        group_name = grp_q.scalar_one_or_none()

    desc = f"Adopted device '{device.name or device_id}'"
    if group_name:
        desc += f" into group '{group_name}'"
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.adopt", resource_type="device",
        resource_id=str(device_id),
        description=desc,
        details={
            "name": device.name,
            "location": device.location,
            "group_id": str(device.group_id) if device.group_id else None,
            "profile_id": str(device.profile_id) if device.profile_id else None,
        },
        request=request,
    )
    await db.commit()

    return {"ok": True}


@router.delete("/{device_id}", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def delete_device(device_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    device = await _get_device_with_access(device_id, request, db)
    device_name = device.name

    # Tell the device to wipe cached assets before we remove it from the DB
    wipe_msg = WipeAssetsMessage(reason="deleted")
    await get_transport().send_to_device(device_id, wipe_msg.model_dump(mode="json"))

    # Remove referencing rows before deleting the device
    from cms.models.asset import DeviceAsset

    await db.execute(
        DeviceAsset.__table__.delete().where(DeviceAsset.device_id == device_id)
    )

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.delete", resource_type="device",
        resource_id=str(device_id),
        description=f"Deleted device '{device_name or device_id}'",
        details={"name": device_name},
        request=request,
    )
    await db.delete(device)
    await db.commit()
    return {"deleted": device_id}


@router.post("/{device_id}/logs", dependencies=[Depends(require_permission(DEVICES_READ))])
async def request_device_logs(
    device_id: str,
    request: Request,
    body: LogRequest = LogRequest(),
    db: AsyncSession = Depends(get_db),
):
    """Request logs from a connected device via WebSocket.

    Returns a dict of {service_name: log_text}.
    """
    device = await _get_device_with_access(device_id, request, db)

    if not get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    try:
        logs = await get_transport().request_logs(device_id, services=body.services, since=body.since)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e))
    except RuntimeError as e:
        # The device reported an error while collecting logs (e.g. journalctl
        # not installed). Surface it as an upstream/bad-gateway response with
        # the device's own error string so the operator can diagnose.
        raise HTTPException(status_code=502, detail=str(e))

    return {"device_id": device_id, "logs": logs}


# ── Groups ──


@router.get("/groups/", response_model=List[DeviceGroupOut], dependencies=[Depends(require_permission(GROUPS_READ))])
async def list_groups(request: Request, db: AsyncSession = Depends(get_db)):
    user = getattr(request.state, "user", None)
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None

    query = (
        select(
            DeviceGroup,
            func.count(Device.id).label("device_count"),
        )
        .outerjoin(Device, Device.group_id == DeviceGroup.id)
        .group_by(DeviceGroup.id)
        .order_by(DeviceGroup.name)
    )
    if not is_admin:
        if group_ids:
            query = query.where(DeviceGroup.id.in_(group_ids))
        else:
            query = query.where(False)

    result = await db.execute(query)
    return [
        DeviceGroupOut(
            id=group.id,
            name=group.name,
            description=group.description,
            default_asset_id=group.default_asset_id,
            device_count=count,
            created_at=group.created_at,
        )
        for group, count in result.all()
    ]


@router.post("/groups/", response_model=DeviceGroupOut, status_code=201, dependencies=[Depends(require_permission(GROUPS_WRITE))])
async def create_group(data: DeviceGroupCreate, request: Request, db: AsyncSession = Depends(get_db)):
    # Gate splash assignment on variant readiness (issue #201).
    if data.default_asset_id:
        await require_asset_ready(db, data.default_asset_id)

    group = DeviceGroup(name=data.name, description=data.description, default_asset_id=data.default_asset_id)
    db.add(group)
    await db.flush()

    # Auto-add non-admin creator to the new group. Without this, the list_groups
    # endpoint (which filters by user_groups for non-admins) would hide the
    # group from its own creator — it looks to the user as if creation failed.
    # Admins are view_all and don't need an explicit membership row.
    user = getattr(request.state, "user", None)
    if user is not None:
        user_group_ids = await get_user_group_ids(user, db)
        if user_group_ids is not None:  # None => admin / view_all, skip
            from cms.models.user import UserGroup
            db.add(UserGroup(user_id=user.id, group_id=group.id))
            await db.flush()

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="group.create", resource_type="group",
        resource_id=str(group.id),
        description=f"Created device group '{group.name}'",
        details={
            "name": group.name,
            "description": group.description,
            "default_asset_id": str(group.default_asset_id) if group.default_asset_id else None,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(group)
    return DeviceGroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        default_asset_id=group.default_asset_id,
        device_count=0,
        created_at=group.created_at,
    )


@router.patch("/groups/{group_id}", response_model=DeviceGroupOut, dependencies=[Depends(require_permission(GROUPS_WRITE))])
async def update_group(
    group_id: uuid.UUID,
    data: DeviceGroupUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(DeviceGroup).where(DeviceGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    user = getattr(request.state, "user", None)
    if user:
        await verify_resource_group_access(user, db, group_id)

    updates = data.model_dump(exclude_unset=True)
    changes = compute_diff(group, updates)

    # Gate splash assignment on variant readiness (issue #201).
    if updates.get("default_asset_id"):
        await require_asset_ready(db, updates["default_asset_id"])

    for field, value in updates.items():
        setattr(group, field, value)
    await audit_log(
        db, user=user,
        action="group.update", resource_type="group",
        resource_id=str(group_id),
        description=f"Modified device group '{group.name}'",
        details={"changes": changes},
        request=request,
    )
    await db.commit()
    await db.refresh(group, ["default_asset"])

    # When default_asset_id changes, push an immediate sync to all group
    # members so they pick up the new asset without waiting for the next
    # scheduler cycle (~15s).  Mirrors the per-device handler behaviour.
    if "default_asset_id" in updates:
        from cms.routers.ws import get_asset_base_url

        base_url = get_asset_base_url(request)
        devices_q = await db.execute(
            select(Device)
            .options(selectinload(Device.default_asset))
            .where(Device.group_id == group.id)
        )
        for device in devices_q.scalars().all():
            # Resolve: device default → group default → none (splash)
            effective_asset = device.default_asset
            if not effective_asset:
                effective_asset = group.default_asset

            if effective_asset:
                await _push_default_asset(device.id, effective_asset, base_url, db)
            else:
                await push_sync_to_device(device.id, db)

    count_q = await db.execute(select(func.count(Device.id)).where(Device.group_id == group.id))
    return DeviceGroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        default_asset_id=group.default_asset_id,
        device_count=count_q.scalar() or 0,
        created_at=group.created_at,
    )


@router.delete("/groups/{group_id}", dependencies=[Depends(require_permission(GROUPS_WRITE))])
async def delete_group(group_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    from cms.models.schedule import Schedule

    result = await db.execute(select(DeviceGroup).where(DeviceGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    user = getattr(request.state, "user", None)
    if user:
        await verify_resource_group_access(user, db, group_id)

    # Block deletion if any schedule references this group
    sched_count = await db.scalar(
        select(func.count()).select_from(Schedule).where(Schedule.group_id == group_id)
    )
    if sched_count:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete — group is used by {sched_count} schedule(s). Remove it from all schedules first.",
        )

    await audit_log(
        db, user=user,
        action="group.delete", resource_type="group",
        resource_id=str(group_id),
        description=f"Deleted device group '{group.name}'",
        details={"name": group.name},
        request=request,
    )
    await db.delete(group)
    await db.commit()
    return {"deleted": str(group_id)}


# ── Device-originated: WPS connect token ────────────────────────────
#
# A device authenticates with its API key and asks the CMS to mint a
# WPS client access token.  Only available when DEVICE_TRANSPORT=wps;
# returns 404 on the direct-WS deployment so devices discover the mode
# automatically.


def _hash_device_api_key(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()


async def _authenticate_device(
    device_id: str, api_key: str | None, db: AsyncSession,
) -> Device:
    """Verify the presented X-Device-API-Key matches `device_id`.

    Returns the Device on success, raises 401/404 otherwise.  Accepts
    the previous key within the standard rotation grace window.
    """
    from datetime import datetime, timedelta, timezone as _tz

    if not api_key:
        raise HTTPException(status_code=401, detail="X-Device-API-Key required")

    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    key_hash = _hash_device_api_key(api_key)
    if device.device_api_key_hash == key_hash:
        return device
    if (
        device.previous_api_key_hash == key_hash
        and device.api_key_rotated_at is not None
    ):
        rotated_at = device.api_key_rotated_at
        if rotated_at.tzinfo is None:
            rotated_at = rotated_at.replace(tzinfo=_tz.utc)
        if datetime.now(_tz.utc) - rotated_at < timedelta(seconds=300):
            return device
    raise HTTPException(status_code=401, detail="Invalid device API key")


@device_originated_router.post("/{device_id}/connect-token")
async def connect_token(
    device_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Mint a WPS client access token (URL + JWT) for `device_id`.

    Auth: `X-Device-API-Key` header bound to `device_id`.
    Behaviour depends on the configured transport:
      - `DEVICE_TRANSPORT=wps`: return {url, token} from the WPS SDK.
      - else: 404 so devices flip back to the direct-WS path.
    """
    settings = get_settings()
    if settings.device_transport != "wps":
        raise HTTPException(status_code=404, detail="WPS transport not enabled")

    api_key = request.headers.get("X-Device-API-Key")
    await _authenticate_device(device_id, api_key, db)

    transport = get_transport()
    if not hasattr(transport, "get_client_access_token"):
        raise HTTPException(
            status_code=500, detail="Transport does not support client tokens",
        )
    minutes = getattr(settings, "wps_token_lifetime_minutes", 60)
    token = await transport.get_client_access_token(device_id, minutes_to_expire=minutes)
    return {
        "url": token.get("url"),
        "token": token.get("token"),
    }