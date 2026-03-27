"""Device management API routes."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import get_settings, require_auth
from cms.database import get_db
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.schemas.device import (
    DeviceGroupCreate,
    DeviceGroupOut,
    DeviceOut,
    DeviceUpdate,
)

router = APIRouter(prefix="/api/devices", dependencies=[Depends(require_auth)])


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
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(device, field, value)
    await db.commit()
    await db.refresh(device, ["group"])

    return DeviceOut(
        **{c.key: getattr(device, c.key) for c in Device.__table__.columns},
        group_name=device.group.name if device.group else None,
    )


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
            device_count=count,
            created_at=group.created_at,
        )
        for group, count in result.all()
    ]


@router.post("/groups/", response_model=DeviceGroupOut, status_code=201)
async def create_group(data: DeviceGroupCreate, db: AsyncSession = Depends(get_db)):
    group = DeviceGroup(name=data.name, description=data.description)
    db.add(group)
    await db.commit()
    await db.refresh(group)
    return DeviceGroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        device_count=0,
        created_at=group.created_at,
    )


@router.delete("/groups/{group_id}")
async def delete_group(group_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(DeviceGroup).where(DeviceGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    await db.delete(group)
    await db.commit()
    return {"deleted": str(group_id)}
