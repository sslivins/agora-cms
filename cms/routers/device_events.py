"""Device event log API — list health events with group-scoped visibility."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import and_, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import (
    build_group_snapshot_read_scope_clause,
    get_current_user,
    get_user_group_ids,
)
from cms.database import get_db
from cms.models.device_event import DeviceEvent
from cms.models.user import User
from cms.schemas.device_event import DeviceEventOut

router = APIRouter(prefix="/api/device-events")

def _base_query(user: User, group_ids: list[uuid.UUID] | None):
    """Build a base query filtered to events the user can see.

    System events (device_id IS NULL, e.g. CMS started/stopped) are always visible.
    Device events are gated by group membership unless user has groups:view_all.
    """
    q = select(DeviceEvent)
    if group_ids is not None:
        q = q.where(
            or_(
                DeviceEvent.device_id.is_(None),  # system events
                build_group_snapshot_read_scope_clause(
                    group_ids,
                    DeviceEvent.group_id,
                    DeviceEvent.group_ids,
                ),
            )
        )
    return q


@router.get("", response_model=list[DeviceEventOut])
async def list_device_events(
    device_id: str | None = None,
    event_type: str | None = None,
    group_id: uuid.UUID | None = None,
    limit: int = Query(default=100, le=500),
    cursor_created_at: datetime | None = None,
    cursor_id: uuid.UUID | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List device events visible to the current user, newest first."""
    gids = await get_user_group_ids(user, db)
    q = _base_query(user, gids)

    if device_id:
        q = q.where(DeviceEvent.device_id == device_id)
    if event_type:
        q = q.where(DeviceEvent.event_type == event_type)
    if group_id:
        q = q.where(
            build_group_snapshot_read_scope_clause(
                {group_id},
                DeviceEvent.group_id,
                DeviceEvent.group_ids,
            )
        )
    if cursor_created_at is not None:
        if cursor_id is None:
            q = q.where(DeviceEvent.created_at < cursor_created_at)
        else:
            q = q.where(
                or_(
                    DeviceEvent.created_at < cursor_created_at,
                    and_(
                        DeviceEvent.created_at == cursor_created_at,
                        DeviceEvent.id < cursor_id,
                    ),
                )
            )

    q = q.order_by(DeviceEvent.created_at.desc(), DeviceEvent.id.desc()).limit(limit)
    result = await db.execute(q)
    return [DeviceEventOut.from_orm_event(ev) for ev in result.scalars().all()]


@router.get("/count")
async def device_event_count(
    device_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return total event count (for pagination / dashboards)."""
    gids = await get_user_group_ids(user, db)
    q = _base_query(user, gids).with_only_columns(func.count(DeviceEvent.id))
    if device_id:
        q = q.where(DeviceEvent.device_id == device_id)
    result = await db.execute(q)
    return {"count": result.scalar() or 0}
