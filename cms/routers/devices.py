"""Device management API routes."""

import uuid
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import get_settings, get_user_group_ids, require_auth, require_permission, verify_resource_group_access
from cms.database import get_db
from cms.permissions import (
    DEVICES_READ, DEVICES_WRITE, DEVICES_MANAGE,
    GROUPS_READ, GROUPS_WRITE,
)
from cms.models.asset import Asset
from shared.models.asset import AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_profile import DeviceProfile
from cms.schemas.device import (
    AdoptRequest,
    DeviceGroupCreate,
    DeviceGroupOut,
    DeviceGroupUpdate,
    DeviceOut,
    DeviceUpdate,
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

# Stage 4 (#344): the in-memory `_upgrading` set was replaced with the
# `devices.upgrade_started_at` column + TTL, so upgrade state is visible
# across replicas and survives restarts.  The timestamp doubles as a
# claim token — the upgrade endpoint captures the value written by its
# atomic CAS and any cleanup compares against that exact timestamp so
# we can't accidentally clear a successor's claim.  ``UPGRADE_TTL`` is
# the max time we'll treat an in-flight upgrade as still valid before
# letting another upgrade request reclaim it (covers stuck reboots).
UPGRADE_TTL = timedelta(minutes=15)


def _is_upgrading(device: Device, *, now: datetime | None = None) -> bool:
    """Return whether *device* has an active upgrade claim within TTL."""
    if device.upgrade_started_at is None:
        return False
    ref = now or datetime.now(timezone.utc)
    return (ref - device.upgrade_started_at) < UPGRADE_TTL

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
    "display_ports",
    "ip_address",
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
    _transport = get_transport()
    live_states = {s["device_id"]: s for s in await _transport.get_all_states()}
    connected_ids = set(await _transport.connected_ids())
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
            is_online=d.id in connected_ids,
            is_upgrading=_is_upgrading(d),
            playback_mode=live_states[d.id]["mode"] if d.id in live_states else None,
            playback_asset=_resolve_asset_name(d.id),
            pipeline_state=live_states[d.id]["pipeline_state"] if d.id in live_states else None,
            display_connected=live_states[d.id]["display_connected"] if d.id in live_states else None,
            display_ports=live_states[d.id]["display_ports"] if d.id in live_states else None,
            cpu_temp_c=live_states[d.id]["cpu_temp_c"] if d.id in live_states else None,
            ip_address=(
                live_states[d.id]["ip_address"]
                if d.id in live_states and live_states[d.id]["ip_address"]
                else d.ip_address
            ),
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
    _transport = get_transport()
    live_states = {s["device_id"]: s for s in await _transport.get_all_states()}
    is_online = await _transport.is_connected(device.id)
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
        is_online=is_online,
        is_upgrading=_is_upgrading(device),
        playback_mode=live_states[device.id]["mode"] if device.id in live_states else None,
        playback_asset=raw_asset,
        pipeline_state=live_states[device.id]["pipeline_state"] if device.id in live_states else None,
        display_connected=live_states[device.id]["display_connected"] if device.id in live_states else None,
        display_ports=live_states[device.id]["display_ports"] if device.id in live_states else None,
        cpu_temp_c=live_states[device.id]["cpu_temp_c"] if device.id in live_states else None,
        ip_address=(
            live_states[device.id]["ip_address"]
            if device.id in live_states and live_states[device.id]["ip_address"]
            else device.ip_address
        ),
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
        # Slideshow defaults require slideshow_v1 capability on the device.
        new_default = await db.get(Asset, updates["default_asset_id"])
        if new_default and new_default.asset_type == AssetType.SLIDESHOW:
            from cms.schemas.protocol import CAPABILITY_SLIDESHOW_V1
            if CAPABILITY_SLIDESHOW_V1 not in (device.capabilities or []):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Slideshow assets require firmware advertising the "
                        "'slideshow_v1' capability. This device is not compatible."
                    ),
                )

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
        is_online=await get_transport().is_connected(device.id),
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

    if not await get_transport().is_connected(device_id):
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

    if not await get_transport().is_connected(device_id):
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

    if not await get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    # Stage 4: atomic claim — set ``upgrade_started_at`` iff it's NULL
    # or older than the TTL.  The timestamp we just wrote is captured
    # via RETURNING so a later failure can compare-and-clear without
    # stomping a successor's claim.
    claim_ts = datetime.now(timezone.utc)
    ttl_cutoff = claim_ts - UPGRADE_TTL
    result = await db.execute(
        update(Device)
        .where(Device.id == device_id)
        .where(
            or_(
                Device.upgrade_started_at.is_(None),
                Device.upgrade_started_at < ttl_cutoff,
            )
        )
        .values(upgrade_started_at=claim_ts)
        .returning(Device.upgrade_started_at)
        .execution_options(synchronize_session=False)
    )
    claimed = result.scalar_one_or_none()
    await db.commit()
    if claimed is None:
        # Another request holds a live claim within TTL.
        raise HTTPException(
            status_code=409,
            detail="Upgrade already in progress for this device",
        )

    upgrade_msg = UpgradeMessage()
    sent = await get_transport().send_to_device(device_id, upgrade_msg.model_dump(mode="json"))
    if not sent:
        # Compare-and-clear using the claimed timestamp as the claim
        # token — if another request reclaimed the slot after TTL
        # expired (or we somehow raced), this leaves their claim alone.
        await db.execute(
            update(Device)
            .where(Device.id == device_id)
            .where(Device.upgrade_started_at == claimed)
            .values(upgrade_started_at=None)
            .execution_options(synchronize_session=False)
        )
        await db.commit()
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

    if not await get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    config_msg = ConfigMessage(ssh_enabled=enabled)
    sent = await get_transport().send_to_device(device_id, config_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    # Track the SSH state immediately so the UI reflects it
    await get_transport().set_state_flags(device_id, ssh_enabled=enabled)

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

    if not await get_transport().is_connected(device_id):
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

    if not await get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    config_msg = ConfigMessage(local_api_enabled=enabled)
    sent = await get_transport().send_to_device(device_id, config_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    await get_transport().set_state_flags(device_id, local_api_enabled=enabled)

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
    try:
        await db.flush()
    except IntegrityError:
        # Unique constraint on ``device_groups.name`` — surface as 409
        # so callers (UI, e2e tests, scripts) can distinguish a
        # duplicate from a real server error.  Race-safe: we let the
        # DB enforce uniqueness rather than do a TOCTOU pre-check.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Device group named '{data.name}' already exists",
        )

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
        # Slideshow group default requires every adopted member to advertise
        # slideshow_v1 — same precedent as the schedule create/update gate.
        new_default = await db.get(Asset, updates["default_asset_id"])
        if new_default and new_default.asset_type == AssetType.SLIDESHOW:
            from cms.schemas.protocol import CAPABILITY_SLIDESHOW_V1
            members_q = await db.execute(
                select(Device).where(
                    Device.group_id == group_id,
                    Device.status == DeviceStatus.ADOPTED,
                )
            )
            incompatible = [
                d for d in members_q.scalars().all()
                if CAPABILITY_SLIDESHOW_V1 not in (d.capabilities or [])
            ]
            if incompatible:
                names = ", ".join(d.name or d.id for d in incompatible[:3])
                suffix = (
                    f" and {len(incompatible) - 3} more"
                    if len(incompatible) > 3 else ""
                )
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Slideshow assets require firmware advertising the "
                        "'slideshow_v1' capability. These devices in the "
                        f"group are not compatible: {names}{suffix}"
                    ),
                )

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


@router.get("/groups/{group_id}/panel", dependencies=[Depends(require_permission(GROUPS_READ))])
async def get_group_panel(group_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Return the rendered <div class='group-panel'> HTML for one group.

    Mirrors the pattern established by GET /api/assets/{id}/row and
    /api/profiles/{id}/row so the cross-session poller on /devices and the
    createGroup handler can insert server-rendered markup instead of
    synthesizing HTML in JS or issuing a full page reload. See issue #87.
    """
    from fastapi.responses import HTMLResponse
    from cms.ui import templates
    from cms.services.variant_view import is_asset_ready as _is_asset_ready
    from cms.models.schedule import Schedule as ScheduleModel

    user = getattr(request.state, "user", None)
    if user:
        await verify_resource_group_access(user, db, group_id)

    result = await db.execute(
        select(DeviceGroup)
        .where(DeviceGroup.id == group_id)
        .options(selectinload(DeviceGroup.devices))
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    # Annotate the same fields the /devices page template expects.
    group.device_count = len(group.devices)
    group.schedule_count = await db.scalar(
        select(func.count()).select_from(ScheduleModel).where(ScheduleModel.group_id == group_id)
    ) or 0

    from cms.services.version_checker import is_update_available
    transport = get_transport()
    live_states = {s["device_id"]: s for s in await transport.get_all_states()}
    for d in group.devices:
        # See cms/ui.py: detach before decorating with live-state attributes
        # so display-only values (cpu_temp_c, ip_address, …) cannot autoflush
        # back to the DB.  Some of these names collide with real columns.
        db.expunge(d)
        d.is_online = await transport.is_connected(d.id)
        state = live_states.get(d.id)
        d.cpu_temp_c = state["cpu_temp_c"] if state else None
        # #436: prefer live LAN IP, fall back to last-known persisted value.
        live_ip = state["ip_address"] if state else None
        d.ip_address = live_ip or d.ip_address
        d.playback_mode = state["mode"] if state else None
        d.playback_asset = state["asset"] if state else None
        d.pipeline_state = state["pipeline_state"] if state else None
        d.started_at = state["started_at"] if state else None
        d.playback_position_ms = state["playback_position_ms"] if state else None
        d.ssh_enabled = state["ssh_enabled"] if state else None
        d.local_api_enabled = state["local_api_enabled"] if state else None
        d.update_available = is_update_available(d.firmware_version)
        d.is_upgrading = _is_upgrading(d)
        d.has_active_schedule = False  # poller will flip this via updateLiveFields

    # Splash-screen dropdown options need the same ready annotations ui.py
    # applies on the full page render.
    assets_q = await db.execute(
        select(Asset)
        .options(selectinload(Asset.variants))
        .where(Asset.deleted_at.is_(None))
        .order_by(Asset.filename)
    )
    assets = assets_q.scalars().all()
    for a in assets:
        ready, reason = _is_asset_ready(a.variants)
        a.ready_for_selection = ready
        a.not_ready_reason = reason

    # All groups the user can see — populates each device row's group-select.
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None
    if is_admin:
        visible_groups = (await db.execute(
            select(DeviceGroup).order_by(DeviceGroup.name)
        )).scalars().all()
    elif group_ids:
        visible_groups = (await db.execute(
            select(DeviceGroup).where(DeviceGroup.id.in_(group_ids)).order_by(DeviceGroup.name)
        )).scalars().all()
    else:
        visible_groups = []

    user_perms = list(user.role.permissions) if user and user.role else []
    pending_ttl_hours = get_settings().pending_device_ttl_hours

    # Phase C: rich device_row needs profiles, latest_version, timezones, and
    # per-device severity_tags + per-group rollup.
    from cms.models.device_profile import DeviceProfile
    from cms.services.version_checker import get_latest_device_version
    from cms.services.device_alerts import device_severity_tags, fleet_counts
    from cms.ui import COMMON_TIMEZONES

    profiles_q = await db.execute(select(DeviceProfile).order_by(DeviceProfile.name))
    profiles = profiles_q.scalars().all()
    latest_version = get_latest_device_version()
    timezones = COMMON_TIMEZONES

    for d in group.devices:
        d.severity_tags = device_severity_tags(d, user_perms)
    group.rollup = fleet_counts(group.devices, user_perms)

    macros = templates.env.get_template("_macros.html").module
    html = macros.group_panel(
        group, user_perms, assets, visible_groups, pending_ttl_hours,
        profiles, latest_version, timezones,
    )
    return HTMLResponse(str(html))


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