"""Notification API routes with scope-based visibility filtering."""

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select, or_
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_current_user
from cms.database import get_db
from cms.models.notification import Notification
from cms.models.user import User, UserGroup
from cms.permissions import NOTIFICATIONS_SYSTEM, GROUPS_VIEW_ALL
from cms.schemas.notification import NotificationCount, NotificationOut

router = APIRouter(prefix="/api/notifications")


def _visibility_filter(user: User):
    """Build SQLAlchemy filter clauses for notifications visible to *user*.

    - scope=system  → requires notifications:system permission
    - scope=group   → user must belong to the group (or have groups:view_all)
    - scope=user    → notification.user_id must match requesting user
    """
    perms = user.role.permissions if user.role else []
    clauses = []

    if NOTIFICATIONS_SYSTEM in perms:
        clauses.append(Notification.scope == "system")

    # Group-scoped: user's group IDs will be resolved in the endpoint
    # We use a placeholder that gets replaced per-query
    clauses.append(Notification.scope == "group")

    clauses.append(
        (Notification.scope == "user") & (Notification.user_id == user.id)
    )

    return or_(*clauses)


async def _user_group_ids(user: User, db: AsyncSession) -> list[uuid.UUID]:
    """Get group IDs the user belongs to."""
    result = await db.execute(
        select(UserGroup.group_id).where(UserGroup.user_id == user.id)
    )
    return [row[0] for row in result.all()]


async def _visible_query(user: User, db: AsyncSession):
    """Return a base select filtered to notifications the user can see."""
    perms = user.role.permissions if user.role else []
    clauses = []

    # System scope — permission gated
    if NOTIFICATIONS_SYSTEM in perms:
        clauses.append(Notification.scope == "system")

    # Group scope — membership or groups:view_all
    if GROUPS_VIEW_ALL in perms:
        clauses.append(Notification.scope == "group")
    else:
        gids = await _user_group_ids(user, db)
        if gids:
            clauses.append(
                (Notification.scope == "group") & (Notification.group_id.in_(gids))
            )

    # User scope — only own notifications
    clauses.append(
        (Notification.scope == "user") & (Notification.user_id == user.id)
    )

    return select(Notification).where(or_(*clauses))


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List notifications visible to the current user, newest first."""
    query = await _visible_query(user, db)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    query = query.order_by(Notification.created_at.desc()).limit(limit)
    result = await db.execute(query)
    return result.scalars().all()


@router.get("/count", response_model=NotificationCount)
async def notification_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return unread notification count for the current user (for polling)."""
    base = await _visible_query(user, db)
    # Replace the selected columns with a count
    count_q = base.where(Notification.read_at.is_(None)).with_only_columns(
        func.count(Notification.id)
    )
    result = await db.execute(count_q)
    return NotificationCount(unread=result.scalar() or 0)


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark a single notification as read."""
    base = await _visible_query(user, db)
    query = base.where(Notification.id == notification_id)
    result = await db.execute(query)
    notif = result.scalar_one_or_none()
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    if notif.read_at is None:
        notif.read_at = datetime.now(timezone.utc)
        await db.commit()
        await db.refresh(notif)
    return notif


@router.post("/read-all")
async def mark_all_read(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark all visible unread notifications as read."""
    base = await _visible_query(user, db)
    query = base.where(Notification.read_at.is_(None))
    result = await db.execute(query)
    notifications = result.scalars().all()
    now = datetime.now(timezone.utc)
    for notif in notifications:
        notif.read_at = now
    await db.commit()
    return {"marked_read": len(notifications)}


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete a single notification."""
    base = await _visible_query(user, db)
    query = base.where(Notification.id == notification_id)
    result = await db.execute(query)
    notif = result.scalar_one_or_none()
    if not notif:
        raise HTTPException(status_code=404, detail="Notification not found")
    await db.delete(notif)
    await db.commit()
    return {"deleted": True}


@router.delete("")
async def delete_all_notifications(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Delete all visible notifications."""
    base = await _visible_query(user, db)
    result = await db.execute(base)
    notifications = result.scalars().all()
    for notif in notifications:
        await db.delete(notif)
    await db.commit()
    return {"deleted": len(notifications)}
