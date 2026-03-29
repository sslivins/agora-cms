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
)
from cms.schemas.protocol import ConfigMessage, FetchAssetMessage, PlayMessage, RebootMessage, SyncMessage, UpgradeMessage
from cms.services.device_manager import device_manager

router = APIRouter(prefix="/api/devices", dependencies=[Depends(require_auth)])


async def _push_default_asset(device_id: str, asset: Asset, base_url: str) -> None:
    """Send fetch_asset + play to a connected device for its default asset."""
    download_url = f"{base_url}/api/assets/{asset.id}/download"
    fetch = FetchAssetMessage(
        asset_name=asset.filename,
        download_url=download_url,
        checksum=asset.checksum,
        size_bytes=asset.size_bytes,
    )
    await device_manager.send_to_device(device_id, fetch.model_dump(mode="json"))
    play = PlayMessage(asset=asset.filename, loop=True)
    await device_manager.send_to_device(device_id, play.model_dump(mode="json"))


# ── Devices ──


@router.get("", response_model=List[DeviceOut])
async def list_devices(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Device).options(selectinload(Device.group)).order_by(Device.registered_at)
    )
    devices = result.scalars().all()
    return [
        DeviceOut(
            **{c.key: getattr(d, c.key) for c in Device.__table__.columns},
            group_name=d.group.name if d.group else None,
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
            await _push_default_asset(device_id, effective_asset, base_url)
        else:
            # No default at any level — tell device to show splash
            sync = SyncMessage(default_asset=None)
            await device_manager.send_to_device(device_id, sync.model_dump(mode="json"))

    return DeviceOut(
        **{c.key: getattr(device, c.key) for c in Device.__table__.columns},
        group_name=device.group.name if device.group else None,
    )


@router.post("/{device_id}/password")
async def set_device_password(
    device_id: str,
    data: dict,
    db: AsyncSession = Depends(get_db),
):
    password = data.get("password", "").strip()
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
        raise HTTPException(status_code=409, detail="Device is not connected")

    upgrade_msg = UpgradeMessage()
    sent = await device_manager.send_to_device(device_id, upgrade_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    return {"ok": True}


@router.post("/{device_id}/reset-auth")
async def reset_device_auth(device_id: str, db: AsyncSession = Depends(get_db)):
    """Clear the device's stored auth token hash.

    Use this when a device has been re-flashed or its SD card replaced.
    The device will be assigned a new token on its next connection.
    """
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    device.device_auth_token_hash = None
    device.device_api_key_hash = None
    device.api_key_rotated_at = None
    await db.commit()

    return {"ok": True}


@router.delete("/{device_id}")
async def delete_device(device_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    await db.delete(device)
    await db.commit()
    return {"deleted": device_id}


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
