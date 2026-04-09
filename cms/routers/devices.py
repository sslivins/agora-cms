"""Device management API routes."""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import get_settings, require_auth
from cms.database import get_db
from cms.models.asset import Asset
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.schemas.device import (
    DeviceGroupCreate,
    DeviceGroupOut,
    DeviceGroupUpdate,
    DeviceOut,
    DeviceUpdate,
    LogRequest,
    SetPasswordRequest,
    ToggleRequest,
)
from cms.schemas.protocol import ConfigMessage, FactoryResetMessage, RebootMessage, SyncMessage, UpgradeMessage
from cms.services.device_manager import device_manager
from cms.services.scheduler import push_sync_to_device
from cms.services.version_checker import check_now

router = APIRouter(prefix="/api/devices", dependencies=[Depends(require_auth)])

# Track devices with an in-flight upgrade to prevent concurrent upgrade commands
_upgrading: set[str] = set()


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
        await device_manager.send_to_device(device_id, fetch.model_dump(mode="json"))

    # Push a fresh sync so the device learns the new default_asset and splash
    # immediately (instead of waiting up to 15s for the scheduler cycle).
    await push_sync_to_device(device_id, db)


# ── Devices ──


@router.post("/check-updates")
async def check_for_updates():
    """Trigger an immediate check for the latest device firmware version."""
    latest = await check_now()
    return {"latest_version": latest}


@router.get("", response_model=List[DeviceOut])
async def list_devices(db: AsyncSession = Depends(get_db)):
    from cms.services.scheduler import get_now_playing

    result = await db.execute(
        select(Device).options(selectinload(Device.group)).order_by(Device.registered_at)
    )
    devices = result.scalars().all()
    live_states = {s["device_id"]: s for s in device_manager.get_all_states()}
    scheduled_device_ids = {np["device_id"] for np in get_now_playing()}
    return [
        DeviceOut(
            **{c.key: getattr(d, c.key) for c in Device.__table__.columns},
            group_name=d.group.name if d.group else None,
            is_online=device_manager.is_connected(d.id),
            is_upgrading=d.id in _upgrading,
            playback_mode=live_states[d.id]["mode"] if d.id in live_states else None,
            playback_asset=live_states[d.id]["asset"] if d.id in live_states else None,
            pipeline_state=live_states[d.id]["pipeline_state"] if d.id in live_states else None,
            display_connected=live_states[d.id]["display_connected"] if d.id in live_states else None,
            has_active_schedule=d.id in scheduled_device_ids,
        )
        for d in devices
    ]


@router.get("/{device_id}", response_model=DeviceOut)
async def get_device(device_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Device).options(selectinload(Device.group)).where(Device.id == device_id)
    )
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    return DeviceOut(
        **{c.key: getattr(device, c.key) for c in Device.__table__.columns},
        group_name=device.group.name if device.group else None,
        is_online=device_manager.is_connected(device.id),
    )


@router.patch("/{device_id}", response_model=DeviceOut)
async def update_device(
    device_id: str,
    data: DeviceUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    updates = data.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(device, field, value)
    await db.commit()
    await db.refresh(device, ["group", "default_asset"])

    # If default_asset_id was changed, resolve effective default and push
    if "default_asset_id" in updates:
        base_url = str(request.base_url).rstrip("/")
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
        **{c.key: getattr(device, c.key) for c in Device.__table__.columns},
        group_name=device.group.name if device.group else None,
        is_online=device_manager.is_connected(device.id),
    )


@router.post("/{device_id}/password")
async def set_device_password(
    device_id: str,
    body: SetPasswordRequest,
    db: AsyncSession = Depends(get_db),
):
    password = body.password.strip()
    if not password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if not device_manager.is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    config_msg = ConfigMessage(web_password=password)
    sent = await device_manager.send_to_device(device_id, config_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    return {"ok": True}


@router.post("/{device_id}/reboot")
async def reboot_device(
    device_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if not device_manager.is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    reboot_msg = RebootMessage()
    sent = await device_manager.send_to_device(device_id, reboot_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    return {"ok": True}


@router.post("/{device_id}/upgrade")
async def upgrade_device(
    device_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if not device_manager.is_connected(device_id):
        _upgrading.discard(device_id)
        raise HTTPException(status_code=409, detail="Device is not connected")

    if device_id in _upgrading:
        raise HTTPException(status_code=409, detail="Upgrade already in progress for this device")

    _upgrading.add(device_id)
    upgrade_msg = UpgradeMessage()
    sent = await device_manager.send_to_device(device_id, upgrade_msg.model_dump(mode="json"))
    if not sent:
        _upgrading.discard(device_id)
        raise HTTPException(status_code=502, detail="Failed to send to device")

    return {"ok": True}


@router.post("/{device_id}/ssh")
async def toggle_device_ssh(
    device_id: str,
    body: ToggleRequest,
    db: AsyncSession = Depends(get_db),
):
    enabled = body.enabled

    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if not device_manager.is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    config_msg = ConfigMessage(ssh_enabled=enabled)
    sent = await device_manager.send_to_device(device_id, config_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    # Track the SSH state immediately so the UI reflects it
    conn = device_manager.get(device_id)
    if conn:
        conn.ssh_enabled = enabled

    return {"ok": True}


@router.post("/{device_id}/factory-reset")
async def factory_reset_device(
    device_id: str,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if not device_manager.is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    msg = FactoryResetMessage()
    sent = await device_manager.send_to_device(device_id, msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    return {"ok": True}


@router.post("/{device_id}/local-api")
async def toggle_device_local_api(
    device_id: str,
    body: ToggleRequest,
    db: AsyncSession = Depends(get_db),
):
    enabled = body.enabled

    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if not device_manager.is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    config_msg = ConfigMessage(local_api_enabled=enabled)
    sent = await device_manager.send_to_device(device_id, config_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    conn = device_manager.get(device_id)
    if conn:
        conn.local_api_enabled = enabled

    return {"ok": True}


@router.post("/{device_id}/adopt")
async def adopt_device(device_id: str, db: AsyncSession = Depends(get_db)):
    """Adopt a pending device or re-adopt an orphaned one.

    For pending devices: sets status to adopted and assigns an auth token on next connect.
    For orphaned devices: clears stored auth credentials so a new token is assigned on reconnect.
    """
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if device.status == DeviceStatus.PENDING:
        device.status = DeviceStatus.ADOPTED
    elif device.status == DeviceStatus.ORPHANED:
        device.device_auth_token_hash = None
        device.device_api_key_hash = None
        device.api_key_rotated_at = None
        device.status = DeviceStatus.ADOPTED
    else:
        raise HTTPException(status_code=400, detail="Device is already adopted")

    await db.commit()

    # Push a fresh sync so the device learns its new status immediately
    # (e.g. the OOBE screen advances from "waiting for adoption" to "adopted").
    await push_sync_to_device(device_id, db)

    return {"ok": True}


@router.delete("/{device_id}")
async def delete_device(device_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    # Remove referencing rows before deleting the device
    from cms.models.asset import DeviceAsset
    from cms.models.schedule import Schedule

    await db.execute(
        DeviceAsset.__table__.delete().where(DeviceAsset.device_id == device_id)
    )
    await db.execute(
        Schedule.__table__.delete().where(Schedule.device_id == device_id)
    )

    await db.delete(device)
    await db.commit()
    return {"deleted": device_id}


@router.post("/{device_id}/logs")
async def request_device_logs(
    device_id: str,
    body: LogRequest = LogRequest(),
    db: AsyncSession = Depends(get_db),
):
    """Request logs from a connected device via WebSocket.

    Returns a dict of {service_name: log_text}.
    """
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    if not device_manager.is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    try:
        logs = await device_manager.request_logs(device_id, services=body.services, since=body.since)
    except TimeoutError as e:
        raise HTTPException(status_code=504, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"device_id": device_id, "logs": logs}


# ── Groups ──


@router.get("/groups/", response_model=List[DeviceGroupOut])
async def list_groups(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(
            DeviceGroup,
            func.count(Device.id).label("device_count"),
        )
        .outerjoin(Device, Device.group_id == DeviceGroup.id)
        .group_by(DeviceGroup.id)
        .order_by(DeviceGroup.name)
    )
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


@router.post("/groups/", response_model=DeviceGroupOut, status_code=201)
async def create_group(data: DeviceGroupCreate, db: AsyncSession = Depends(get_db)):
    group = DeviceGroup(name=data.name, description=data.description, default_asset_id=data.default_asset_id)
    db.add(group)
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


@router.patch("/groups/{group_id}", response_model=DeviceGroupOut)
async def update_group(
    group_id: uuid.UUID,
    data: DeviceGroupUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(DeviceGroup).where(DeviceGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(group, field, value)
    await db.commit()
    await db.refresh(group)
    count_q = await db.execute(select(func.count(Device.id)).where(Device.group_id == group.id))
    return DeviceGroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        default_asset_id=group.default_asset_id,
        device_count=count_q.scalar() or 0,
        created_at=group.created_at,
    )


@router.delete("/groups/{group_id}")
async def delete_group(group_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DeviceGroup).where(DeviceGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    await db.delete(group)
    await db.commit()
    return {"deleted": str(group_id)}
