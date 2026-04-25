"""Schedule CRUD API routes."""

import logging
import uuid
from datetime import time as dt_time, timedelta
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import require_auth, require_permission, get_user_group_ids, verify_resource_group_access
from cms.database import get_db
from cms.permissions import SCHEDULES_READ, SCHEDULES_WRITE
from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.schedule_log import ScheduleLog, ScheduleLogEvent
from cms.schemas.schedule import ScheduleCreate, ScheduleOut, ScheduleUpdate
from cms.services.scheduler import push_sync_to_affected_devices, push_sync_to_device, _get_target_device_ids, skip_schedule_until, clear_schedule_skip, clear_sync_hash, schedules_conflict, evaluate_schedules
from cms.services.audit_service import audit_log, compute_diff
from cms.services.asset_readiness import require_asset_ready

logger = logging.getLogger("agora.cms.schedules")

router = APIRouter(prefix="/api/schedules", dependencies=[Depends(require_auth)])


async def _trigger_eval():
    """Best-effort immediate scheduler eval for responsive dashboard updates."""
    try:
        await evaluate_schedules()
    except Exception:
        logger.debug("Immediate eval after schedule change failed (background loop will catch up)", exc_info=True)


def _schedule_to_out(s: Schedule) -> ScheduleOut:
    return ScheduleOut(
        **{c.key: getattr(s, c.key) for c in Schedule.__table__.columns},
        asset_filename=(s.asset.display_name or s.asset.original_filename or s.asset.filename) if s.asset else None,
        group_name=s.group.name if s.group else None,
    )


def _eager_options():
    return [
        selectinload(Schedule.asset),
        selectinload(Schedule.group),
    ]


async def _verify_schedule_access(schedule: Schedule, request: Request, db) -> None:
    """Verify the current user has group access to the schedule's target."""
    user = getattr(request.state, "user", None)
    if not user:
        return
    if schedule.group_id:
        await verify_resource_group_access(user, db, schedule.group_id)


@router.get("", response_model=List[ScheduleOut], dependencies=[Depends(require_permission(SCHEDULES_READ))])
async def list_schedules(request: Request, db: AsyncSession = Depends(get_db)):
    user = getattr(request.state, "user", None)
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None

    query = select(Schedule).options(*_eager_options()).order_by(Schedule.priority.desc(), Schedule.name)
    if not is_admin:
        if group_ids:
            # Include schedules targeting the user's groups
            query = query.where(
                Schedule.group_id.in_(group_ids)
            )
        else:
            query = query.where(False)

    result = await db.execute(query)
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
    if schedule.group_id:
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


def _compute_end_time(start_time, loop_count: int, duration_seconds: float) -> dt_time:
    """Compute end_time from start_time + loop_count × asset duration."""
    total_seconds = int(loop_count * duration_seconds)
    start_td = timedelta(
        hours=start_time.hour, minutes=start_time.minute, seconds=start_time.second,
    )
    end_td = start_td + timedelta(seconds=total_seconds)
    end_seconds = int(end_td.total_seconds()) % 86400
    h, remainder = divmod(end_seconds, 3600)
    m, s = divmod(remainder, 60)
    return dt_time(h, m, s)


def _is_pi5_compatible(device_type: str) -> bool:
    """Check if a device type string indicates a Pi 5 or Compute Module 5."""
    if not device_type:
        return False
    dt_lower = device_type.lower()
    return "pi 5" in dt_lower or "compute module 5" in dt_lower


async def _validate_webpage_group(group_id: uuid.UUID, db: AsyncSession) -> None:
    """Validate all adopted devices in a group are Pi 5+ for webpage assets."""
    result = await db.execute(
        select(Device).where(
            Device.group_id == group_id,
            Device.status == DeviceStatus.ADOPTED,
        )
    )
    devices = result.scalars().all()
    non_pi5 = [d for d in devices if not _is_pi5_compatible(d.device_type)]
    if non_pi5:
        names = ", ".join(d.name or d.id for d in non_pi5[:3])
        suffix = f" and {len(non_pi5) - 3} more" if len(non_pi5) > 3 else ""
        raise HTTPException(
            status_code=422,
            detail=f"Webpage assets require Raspberry Pi 5 or newer. "
                   f"These devices in the group are not compatible: {names}{suffix}",
        )


async def _resolve_loop_end_time(
    loop_count: int | None,
    asset_id: uuid.UUID,
    start_time,
    db: AsyncSession,
) -> dt_time | None:
    """If loop_count is set, compute end_time from the asset duration.

    Returns the computed end_time, or None if loop_count is not set.
    Raises 422 if the asset is missing or has no duration.
    """
    if loop_count is None:
        return None
    asset = await db.get(Asset, asset_id)
    if not asset:
        raise HTTPException(status_code=422, detail="Asset not found")
    if not asset.duration_seconds:
        raise HTTPException(
            status_code=422,
            detail="Cannot compute end_time: asset has no duration. Provide end_time explicitly.",
        )
    return _compute_end_time(start_time, loop_count, asset.duration_seconds)


@router.post("", response_model=ScheduleOut, status_code=201, dependencies=[Depends(require_permission(SCHEDULES_WRITE))])
async def create_schedule(data: ScheduleCreate, request: Request, db: AsyncSession = Depends(get_db)):
    fields = data.model_dump()
    fields["name"] = await _unique_name(fields["name"], db)

    # Gate on variant readiness for new schedules (issue #201).
    # Webpage/stream assets have no variants and are implicitly ready.
    await require_asset_ready(db, data.asset_id)

    # Check if asset is a webpage type
    asset = await db.get(Asset, data.asset_id)
    if not asset:
        raise HTTPException(status_code=422, detail="Asset not found")

    is_webpage = asset.asset_type == AssetType.WEBPAGE
    is_live_stream = asset.asset_type == AssetType.STREAM
    is_url_asset = is_webpage or is_live_stream

    # Webpage/live-stream assets cannot use loop_count (no duration)
    if is_url_asset and data.loop_count is not None:
        raise HTTPException(
            status_code=422,
            detail="Webpage and live stream assets do not support loop count (no duration).",
        )

    # Webpage/live-stream assets require Pi 5+ devices
    if is_url_asset and data.group_id:
        await _validate_webpage_group(data.group_id, db)

    # Auto-compute end_time when loop_count is set
    if not is_url_asset:
        computed = await _resolve_loop_end_time(
            data.loop_count, data.asset_id, data.start_time, db,
        )
        if computed is not None:
            fields["end_time"] = computed
        elif fields.get("end_time") is None:
            raise HTTPException(
                status_code=422,
                detail="end_time is required when loop_count is not set.",
            )
    else:
        # Webpage: loop_count not applicable, but end_time is required
        if fields.get("end_time") is None:
            raise HTTPException(
                status_code=422,
                detail="end_time is required for webpage/stream assets.",
            )

    schedule = Schedule(**fields)
    await _check_conflicts(schedule, db)
    db.add(schedule)
    await db.flush()
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="schedule.create", resource_type="schedule",
        resource_id=str(schedule.id),
        description=f"Created schedule '{schedule.name}'",
        details={
            "name": schedule.name,
            "asset_id": str(schedule.asset_id),
            "group_id": str(schedule.group_id) if schedule.group_id else None,
            "priority": schedule.priority,
            "enabled": schedule.enabled,
        },
        request=request,
    )
    await db.commit()
    result = await db.execute(
        select(Schedule).options(*_eager_options()).where(Schedule.id == schedule.id)
    )
    schedule = result.scalar_one()
    await push_sync_to_affected_devices(schedule, db)
    await _trigger_eval()
    return _schedule_to_out(schedule)


@router.get("/{schedule_id}", response_model=ScheduleOut, dependencies=[Depends(require_permission(SCHEDULES_READ))])
async def get_schedule(schedule_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Schedule).options(*_eager_options()).where(Schedule.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await _verify_schedule_access(schedule, request, db)
    return _schedule_to_out(schedule)


@router.get("/{schedule_id}/row", dependencies=[Depends(require_permission(SCHEDULES_READ))])
async def get_schedule_row(schedule_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Return just the rendered <tr> HTML for a single schedule.

    Used by the no-reload flows on the schedules page (create, edit, and
    the cross-replica poller) so the client never has to synthesize row
    markup in JS. See issue #87 and the long-term plan captured on it —
    we want a single Jinja source of truth for every row template.
    """
    from fastapi.responses import HTMLResponse
    from cms.ui import templates

    result = await db.execute(
        select(Schedule).options(*_eager_options()).where(Schedule.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await _verify_schedule_access(schedule, request, db)

    user = getattr(request.state, "user", None)
    user_perms = list(user.role.permissions) if user and user.role else []

    macros = templates.env.get_template("_macros.html").module
    html = macros.active_schedule_row(schedule, user_perms)
    return HTMLResponse(str(html))


@router.patch("/{schedule_id}", response_model=ScheduleOut, dependencies=[Depends(require_permission(SCHEDULES_WRITE))])
async def update_schedule(
    schedule_id: uuid.UUID,
    data: ScheduleUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Schedule).where(Schedule.id == schedule_id))
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await _verify_schedule_access(schedule, request, db)

    updates = data.model_dump(exclude_unset=True)

    # Gate swaps to a new asset on variant readiness (issue #201).
    if "asset_id" in updates and updates["asset_id"] != schedule.asset_id:
        await require_asset_ready(db, updates["asset_id"])

    # Snapshot diff before any mutation or auto-computation
    changes = compute_diff(schedule, updates)

    # Check if the resulting asset is a webpage/stream type (current or updated)
    target_asset_id = updates.get("asset_id", schedule.asset_id)
    target_asset = await db.get(Asset, target_asset_id)
    is_webpage = target_asset and target_asset.asset_type == AssetType.WEBPAGE
    is_live_stream = target_asset and target_asset.asset_type == AssetType.STREAM
    is_url_asset = is_webpage or is_live_stream

    # Webpage/live-stream assets cannot use loop_count
    if is_url_asset and updates.get("loop_count") is not None:
        raise HTTPException(
            status_code=422,
            detail="Webpage and live stream assets do not support loop count (no duration).",
        )

    # Validate Pi5 when switching to a webpage/stream asset or changing the group
    if is_url_asset and ("asset_id" in updates or "group_id" in updates):
        target_group_id = updates.get("group_id", schedule.group_id)
        if target_group_id:
            await _validate_webpage_group(target_group_id, db)

    # Recompute end_time when loop_count changes (or when asset/start_time
    # change on a schedule that already has loop_count set).
    loop_count = updates.get("loop_count", schedule.loop_count)
    if not is_url_asset and loop_count is not None and (
        "loop_count" in updates or "asset_id" in updates or "start_time" in updates
    ):
        asset_id = updates.get("asset_id", schedule.asset_id)
        start_time = updates.get("start_time", schedule.start_time)
        computed = await _resolve_loop_end_time(loop_count, asset_id, start_time, db)
        if computed is not None:
            updates["end_time"] = computed

    for field, value in updates.items():
        setattr(schedule, field, value)
    # Clear any active "End Now" skip so the schedule is re-evaluated
    schedule.skipped_until = None
    await _check_conflicts(schedule, db, exclude_id=schedule_id)
    # NB: compute diff against the CURRENT (post-mutation) schedule by
    # comparing against the old snapshot is messier here because end_time
    # may be auto-computed.  Instead we snapshot keys ahead of mutation.
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="schedule.update", resource_type="schedule",
        resource_id=str(schedule_id),
        description=f"Modified schedule '{schedule.name}'",
        details={"changes": changes},
        request=request,
    )
    await db.commit()

    clear_schedule_skip(str(schedule_id))

    result = await db.execute(
        select(Schedule).options(*_eager_options()).where(Schedule.id == schedule.id)
    )
    schedule = result.scalar_one()
    await push_sync_to_affected_devices(schedule, db)
    await _trigger_eval()
    return _schedule_to_out(schedule)


@router.delete("/{schedule_id}", dependencies=[Depends(require_permission(SCHEDULES_WRITE))])
async def delete_schedule(schedule_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Schedule).options(*_eager_options()).where(Schedule.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await _verify_schedule_access(schedule, request, db)
    schedule_name = schedule.name
    # Resolve target devices before deleting the schedule
    target_ids = await _get_target_device_ids(schedule, db)
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="schedule.delete", resource_type="schedule",
        resource_id=str(schedule_id),
        description=f"Deleted schedule '{schedule_name}'",
        details={"name": schedule_name},
        request=request,
    )
    await db.delete(schedule)
    await db.commit()
    # Clear confirmed-playing entries for this schedule immediately
    from cms.services.scheduler import clear_now_playing, _confirmed_playing
    stale = [did for did, info in _confirmed_playing.items()
             if info.get("schedule_id") == str(schedule_id)]
    for did in stale:
        clear_now_playing(did)
    # Push updated sync (without the deleted schedule) to affected devices
    for did in target_ids:
        await push_sync_to_device(did, db)
    await _trigger_eval()
    return {"deleted": str(schedule_id)}


@router.post("/{schedule_id}/end-now", dependencies=[Depends(require_permission(SCHEDULES_WRITE))])
async def end_schedule_now(
    schedule_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """End the current occurrence of a schedule immediately.

    Body (optional): ``{"device_id": "<id>"}`` — when provided, skips the
    schedule only for that device; other devices keep playing.  When omitted,
    the schedule is skipped for every target (legacy behavior).

    The skip runs until the schedule's ``end_time`` today (or tomorrow for
    overnight spans), then resumes on its next regular occurrence.
    """
    from datetime import datetime, timezone
    from zoneinfo import ZoneInfo
    from cms.models.setting import CMSSetting
    from cms.models.schedule_device_skip import ScheduleDeviceSkip

    # Optional JSON body with device_id scoping
    device_id: str | None = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            raw = body.get("device_id")
            if isinstance(raw, str) and raw.strip():
                device_id = raw.strip()
    except Exception:
        device_id = None

    result = await db.execute(
        select(Schedule).options(*_eager_options()).where(Schedule.id == schedule_id)
    )
    schedule = result.scalar_one_or_none()
    if not schedule:
        raise HTTPException(status_code=404, detail="Schedule not found")
    await _verify_schedule_access(schedule, request, db)

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

    # Resolve targets so we can log + push, and (for per-device) verify scope
    from cms.models.device import Device
    target_ids = await _get_target_device_ids(schedule, db)
    if device_id is not None and device_id not in target_ids:
        raise HTTPException(
            status_code=400,
            detail="Device is not a target of this schedule",
        )

    affected_ids = [device_id] if device_id else target_ids

    skip_schedule_until(str(schedule.id), end_today, device_id=device_id)

    # Persist to DB so it survives restarts
    if device_id is None:
        schedule.skipped_until = end_today
        db.add(schedule)
    else:
        # Upsert a per-device skip row
        existing = await db.execute(
            select(ScheduleDeviceSkip).where(
                (ScheduleDeviceSkip.schedule_id == schedule.id)
                & (ScheduleDeviceSkip.device_id == device_id)
            )
        )
        row = existing.scalar_one_or_none()
        if row is None:
            db.add(ScheduleDeviceSkip(
                schedule_id=schedule.id,
                device_id=device_id,
                skip_until=end_today,
            ))
        else:
            row.skip_until = end_today
            db.add(row)

    if device_id is None:
        scope = "all devices"
    else:
        dev = await db.get(Device, device_id)
        label = dev.name if dev and dev.name else device_id
        scope = f"device '{label}'"
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="schedule.end_now", resource_type="schedule",
        resource_id=str(schedule_id),
        description=f"Ended schedule '{schedule.name}' early for {scope} (resumes at {end_today.isoformat()})",
        details={
            "resumes_after": end_today.isoformat(),
            "device_id": device_id,
        },
        request=request,
    )
    await db.commit()

    # Log SKIPPED event for each affected device
    if affected_ids:
        name_q = await db.execute(
            select(Device.id, Device.name).where(Device.id.in_(affected_ids))
        )
        dev_names = {r[0]: (r[1] or r[0]) for r in name_q.all()}
        for did in affected_ids:
            db.add(ScheduleLog(
                schedule_id=schedule.id,
                schedule_name=schedule.name,
                device_id=did,
                device_name=dev_names.get(did, did),
                asset_filename=schedule.asset.display_name or schedule.asset.original_filename or schedule.asset.filename,
                event=ScheduleLogEvent.SKIPPED,
                details="Ended early by admin",
            ))
        await db.commit()

    # Clear sync hash and re-push so affected devices drop this schedule immediately
    for did in affected_ids:
        clear_sync_hash(did)
        await push_sync_to_device(did, db)

    return {
        "ended": str(schedule_id),
        "resumes_after": end_today.isoformat(),
        "device_id": device_id,
    }
