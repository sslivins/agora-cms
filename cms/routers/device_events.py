"""Device event log API — list health events with group-scoped visibility."""

import uuid

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_current_user
from cms.database import get_db
from cms.models.device_event import DeviceEvent
from cms.models.user import User, UserGroup
from cms.permissions import GROUPS_VIEW_ALL
from cms.schemas.device_event import DeviceEventOut

router = APIRouter(prefix="/api/device-events")


async def _user_group_ids(user: User, db: AsyncSession) -> list[uuid.UUID]:
    result = await db.execute(
        select(UserGroup.group_id).where(UserGroup.user_id == user.id)
    )
    return [row[0] for row in result.all()]


def _base_query(user: User, group_ids: list[uuid.UUID]):
    """Build a base query filtered to events the user can see.

    System events (device_id IS NULL, e.g. CMS started/stopped) are always visible.
    Device events are gated by group membership unless user has groups:view_all.
    """
    from sqlalchemy import or_
    perms = user.role.permissions if user.role else []
    q = select(DeviceEvent)
    if GROUPS_VIEW_ALL not in perms:
        if group_ids:
            q = q.where(
                or_(
                    DeviceEvent.device_id.is_(None),           # system events
                    DeviceEvent.group_id.in_(group_ids),       # user's groups
                )
            )
        else:
            # User has no groups — show only system events
            q = q.where(DeviceEvent.device_id.is_(None))
    return q


@router.get("", response_model=list[DeviceEventOut])
async def list_device_events(
    device_id: str | None = None,
    event_type: str | None = None,
    group_id: uuid.UUID | None = None,
    limit: int = Query(default=100, le=500),
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List device events visible to the current user, newest first."""
    gids = await _user_group_ids(user, db)
    q = _base_query(user, gids)

    if device_id:
        q = q.where(DeviceEvent.device_id == device_id)
    if event_type:
        q = q.where(DeviceEvent.event_type == event_type)
    if group_id:
        q = q.where(DeviceEvent.group_id == group_id)

    q = q.order_by(DeviceEvent.created_at.desc()).limit(limit)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/count")
async def device_event_count(
    device_id: str | None = None,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return total event count (for pagination / dashboards)."""
    gids = await _user_group_ids(user, db)
    q = _base_query(user, gids).with_only_columns(func.count(DeviceEvent.id))
    if device_id:
        q = q.where(DeviceEvent.device_id == device_id)
    result = await db.execute(q)
    return {"count": result.scalar() or 0}
