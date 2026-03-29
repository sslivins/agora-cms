"""Schedule CRUD API routes."""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import require_auth
from cms.database import get_db
from cms.models.schedule import Schedule
from cms.schemas.schedule import ScheduleCreate, ScheduleOut, ScheduleUpdate
from cms.services.scheduler import push_sync_to_affected_devices, push_sync_to_device, _get_target_device_ids

router = APIRouter(prefix="/api/schedules", dependencies=[Depends(require_auth)])


def _schedule_to_out(s: Schedule) -> ScheduleOut:
    return ScheduleOut(
        **{c.key: getattr(s, c.key) for c in Schedule.__table__.columns},
        asset_filename=s.asset.filename if s.asset else None,
        device_name=s.device.name if s.device else None,
        group_name=s.group.name if s.group else None,
    )


def _eager_options():
    return [
        selectinload(Schedule.asset),
        selectinload(Schedule.device),
        selectinload(Schedule.group),
    ]


@router.get("", response_model=List[ScheduleOut])
async def list_schedules(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Schedule).options(*_eager_options()).order_by(Schedule.priority.desc(), Schedule.name)
    )
    return [_schedule_to_out(s) for s in result.scalars().all()]


@router.post("", response_model=ScheduleOut, status_code=201)
async def create_schedule(data: ScheduleCreate, db: AsyncSession = Depends(get_db)):
    schedule = Schedule(**data.model_dump())
    db.add(schedule)
    await db.commit()
    result = await db.execute(
        select(Schedule).options(*_eager_options()).where(Schedule.id == schedule.id)
    )
    schedule = result.scalar_one()
    await push_sync_to_affected_devices(schedule, db)
    return _schedule_to_out(schedule)


@router.get("/{schedule_id}", response_model=ScheduleOut)
async def get_schedule(schedule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Schedule).options(*_eager_options()).where(Schedule.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    return _schedule_to_out(schedule)


@router.patch("/{schedule_id}", response_model=ScheduleOut)
async def update_schedule(
    schedule_id: uuid.UUID,
    data: ScheduleUpdate,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Schedule).where(Schedule.id == schedule_id))
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    for field, value in data.model_dump(exclude_unset=True).items():
        setattr(schedule, field, value)
    await db.commit()

    result = await db.execute(
        select(Schedule).options(*_eager_options()).where(Schedule.id == schedule.id)
    )
    schedule = result.scalar_one()
    await push_sync_to_affected_devices(schedule, db)
    return _schedule_to_out(schedule)


@router.delete("/{schedule_id}")
async def delete_schedule(schedule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Schedule).options(*_eager_options()).where(Schedule.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    # Resolve target devices before deleting the schedule
    target_ids = await _get_target_device_ids(schedule, db)
    await db.delete(schedule)
    await db.commit()
    # Push updated sync (without the deleted schedule) to affected devices
    for did in target_ids:
        await push_sync_to_device(did, db)
    return {"deleted": str(schedule_id)}
