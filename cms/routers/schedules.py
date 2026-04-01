"""Schedule CRUD API routes."""

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import require_auth
from cms.database import get_db
from cms.models.schedule import Schedule
from cms.schemas.schedule import ScheduleCreate, ScheduleOut, ScheduleUpdate
from cms.services.scheduler import push_sync_to_affected_devices, push_sync_to_device, _get_target_device_ids, skip_schedule_until, clear_sync_hash, schedules_conflict

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


async def _unique_name(name: str, db: AsyncSession, exclude_id=None) -> str:
    """Append (2), (3), etc. if a schedule with this name already exists."""
    q = select(func.count()).select_from(Schedule).where(Schedule.name == name)
    if exclude_id:
        q = q.where(Schedule.id != exclude_id)
    count = (await db.execute(q)).scalar() or 0
    if count == 0:
        return name
    suffix = 2
    while True:
        candidate = f"{name} ({suffix})"
        q2 = select(func.count()).select_from(Schedule).where(Schedule.name == candidate)
        if exclude_id:
            q2 = q2.where(Schedule.id != exclude_id)
        if (await db.execute(q2)).scalar() == 0:
            return candidate
        suffix += 1


async def _check_conflicts(schedule: Schedule, db: AsyncSession, exclude_id=None):
    """Raise 409 if an existing schedule conflicts (same target, priority, overlapping window)."""
    q = select(Schedule).where(Schedule.enabled == True)
    if schedule.device_id:
        q = q.where(Schedule.device_id == schedule.device_id)
    elif schedule.group_id:
        q = q.where(Schedule.group_id == schedule.group_id)
    else:
        return
    q = q.where(Schedule.priority == schedule.priority)
    if exclude_id:
        q = q.where(Schedule.id != exclude_id)
    result = await db.execute(q)
    for existing in result.scalars().all():
        if schedules_conflict(schedule, existing):
            raise HTTPException(
                status_code=409,
                detail=f"Conflicts with '{existing.name}' — overlapping time on the same target at priority {schedule.priority}. Use a different priority to allow overlap.",
            )


@router.post("", response_model=ScheduleOut, status_code=201)
async def create_schedule(data: ScheduleCreate, db: AsyncSession = Depends(get_db)):
    fields = data.model_dump()
    fields["name"] = await _unique_name(fields["name"], db)
    schedule = Schedule(**fields)
    await _check_conflicts(schedule, db)
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
    await _check_conflicts(schedule, db, exclude_id=schedule_id)
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


@router.post("/{schedule_id}/end-now")
async def end_schedule_now(schedule_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    """End the current occurrence of a schedule immediately.

    The schedule is skipped until its end_time today (or tomorrow for
    overnight spans), then resumes on its next regular occurrence.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from cms.models.setting import CMSSetting

    result = await db.execute(
        select(Schedule).options(*_eager_options()).where(Schedule.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")

    # Determine skip-until as end_time today (local timezone)
    tz_result = await db.execute(
        select(CMSSetting.value).where(CMSSetting.key == "timezone")
    )
    tz_name = tz_result.scalar_one_or_none() or "UTC"
    tz = ZoneInfo(tz_name)
    local_now = datetime.now(timezone.utc).astimezone(tz).replace(tzinfo=None)

    from datetime import timedelta
    end_today = datetime.combine(local_now.date(), schedule.end_time)
    # For overnight schedules (end < start), the end is tomorrow
    if schedule.end_time <= schedule.start_time:
        end_today += timedelta(days=1)

    skip_schedule_until(str(schedule.id), end_today)

    # Clear sync hash and re-push so devices drop this schedule immediately
    target_ids = await _get_target_device_ids(schedule, db)
    for did in target_ids:
        clear_sync_hash(did)
        await push_sync_to_device(did, db)

    return {"ended": str(schedule_id), "resumes_after": end_today.isoformat()}
