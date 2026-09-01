"""Notification API routes with scope-based visibility filtering."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import and_, exists, func, literal, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased, selectinload

from cms.auth import get_current_user
from cms.database import get_db, get_engine
from cms.models.notification import Notification, NotificationGroup, NotificationRead
from cms.models.user import User, UserGroup
from cms.permissions import GROUPS_VIEW_ALL, NOTIFICATIONS_SYSTEM
from cms.schemas.notification import NotificationCount, NotificationOut

router = APIRouter(prefix="/api/notifications")


async def _user_group_ids(user: User, db: AsyncSession) -> list[uuid.UUID]:
    """Get group IDs the user belongs to."""
    result = await db.execute(
        select(UserGroup.group_id).where(UserGroup.user_id == user.id)
    )
    return [row[0] for row in result.all()]


async def _visibility_clause(user: User, db: AsyncSession):
    """Return the read-visibility clause for ``Notification`` rows."""
    perms = user.role.permissions if user.role else []
    clauses = []

    if NOTIFICATIONS_SYSTEM in perms:
        clauses.append(Notification.scope == "system")

    if GROUPS_VIEW_ALL in perms:
        clauses.append(
            and_(
                Notification.scope == "group",
                or_(
                    exists(
                        select(1).where(
                            NotificationGroup.notification_id == Notification.id
                        )
                    ),
                    Notification.group_id.is_not(None),
                ),
            )
        )
    else:
        gids = await _user_group_ids(user, db)
        if gids:
            clauses.append(
                and_(
                    Notification.scope == "group",
                    or_(
                        exists(
                            select(1).where(
                                NotificationGroup.notification_id == Notification.id,
                                NotificationGroup.group_id.in_(gids),
                            )
                        ),
                        Notification.group_id.in_(gids),
                    ),
                )
            )

    clauses.append(
        and_(Notification.scope == "user", Notification.user_id == user.id)
    )
    return or_(*clauses)


def _read_upsert():
    engine = get_engine()
    dialect = engine.dialect.name if engine is not None else "sqlite"
    if dialect == "postgresql":
        return pg_insert(NotificationRead)
    return sqlite_insert(NotificationRead)


def _effective_read_at_clause(read_alias):
    return func.coalesce(read_alias.read_at, Notification.read_at)


def _engine_dialect_name() -> str:
    engine = get_engine()
    return engine.dialect.name if engine is not None else "sqlite"


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


async def _visible_query(
    user: User,
    db: AsyncSession,
):
    """Return ``(query, read_alias)`` for visible notifications."""
    read_row = aliased(NotificationRead)
    query = (
        select(Notification, read_row.read_at, read_row.dismissed_at)
        .options(selectinload(Notification.group_targets))
        .outerjoin(
            read_row,
            and_(
                read_row.notification_id == Notification.id,
                read_row.user_id == user.id,
            ),
        )
        .where(await _visibility_clause(user, db))
        .where(read_row.dismissed_at.is_(None))
    )
    return query, read_row


async def _get_visible_notification(
    notification_id: uuid.UUID,
    *,
    user: User,
    db: AsyncSession,
) -> tuple[Notification, datetime | None]:
    query, read_row = await _visible_query(user, db)
    result = await db.execute(query.where(Notification.id == notification_id))
    row = result.first()
    if row is None:
        raise HTTPException(status_code=404, detail="Notification not found")
    notification, read_at, _dismissed_at = row
    return notification, _normalize_utc(read_at or notification.read_at)


def _set_legacy_user_read_at(notification: Notification, read_at: datetime) -> None:
    """Preserve row-level read state only for single-user notifications."""
    if notification.scope == "user":
        notification.read_at = read_at


@router.get("", response_model=list[NotificationOut])
async def list_notifications(
    unread_only: bool = False,
    limit: int = 50,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List notifications visible to the current user, newest first."""
    query, read_row = await _visible_query(user, db)
    if unread_only:
        query = query.where(_effective_read_at_clause(read_row).is_(None))
    query = query.order_by(Notification.created_at.desc(), Notification.id.desc()).limit(limit)
    result = await db.execute(query)
    return [
        NotificationOut.from_notification(notification, read_at=read_at or notification.read_at)
        for notification, read_at, _dismissed_at in result.unique().all()
    ]


@router.get("/count", response_model=NotificationCount)
async def notification_count(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return unread notification count for the current user (for polling)."""
    query, read_row = await _visible_query(user, db)
    count_q = query.where(_effective_read_at_clause(read_row).is_(None)).with_only_columns(
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
    """Mark a single notification as read for the current user."""
    notification, existing_read_at = await _get_visible_notification(
        notification_id, user=user, db=db,
    )
    read_at = existing_read_at or datetime.now(timezone.utc)
    stmt = _read_upsert().values(
        notification_id=notification_id,
        user_id=user.id,
        read_at=read_at,
        dismissed_at=None,
    ).on_conflict_do_update(
        index_elements=["notification_id", "user_id"],
        set_={"read_at": read_at},
    )
    await db.execute(stmt)
    _set_legacy_user_read_at(notification, read_at)
    await db.commit()
    await db.refresh(notification)
    return NotificationOut.from_notification(
        notification, read_at=_normalize_utc(read_at)
    )


@router.post("/read-all")
async def mark_all_read(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Mark all visible unread notifications as read."""
    query, read_row = await _visible_query(user, db)
    unread_ids = (
        query.where(_effective_read_at_clause(read_row).is_(None))
        .with_only_columns(Notification.id)
        .subquery()
    )
    unread_notification_ids = list((await db.scalars(select(unread_ids.c.id))).all())
    marked_read = len(unread_notification_ids)
    if marked_read:
        now = datetime.now(timezone.utc)
        if _engine_dialect_name() == "postgresql":
            stmt = _read_upsert().from_select(
                ["notification_id", "user_id", "read_at", "dismissed_at"],
                select(
                    unread_ids.c.id,
                    literal(user.id),
                    literal(now),
                    literal(None),
                ),
            ).on_conflict_do_update(
                index_elements=["notification_id", "user_id"],
                set_={"read_at": now},
            )
            await db.execute(stmt)
        else:
            values = [
                {
                    "notification_id": notification_id,
                    "user_id": user.id,
                    "read_at": now,
                    "dismissed_at": None,
                }
                for notification_id in (
                    await db.scalars(select(unread_ids.c.id))
                ).all()
            ]
            if values:
                await db.execute(
                    sqlite_insert(NotificationRead)
                    .values(values)
                    .on_conflict_do_nothing(
                        index_elements=["notification_id", "user_id"]
                    )
                )
                await db.execute(
                    NotificationRead.__table__.update()
                    .where(NotificationRead.user_id == user.id)
                    .where(
                        NotificationRead.notification_id.in_(
                            unread_notification_ids
                        )
                    )
                    .values(read_at=now)
                )

        # Preserve legacy semantics for single-user notifications whose only
        # reader is the addressed user.
        await db.execute(
            Notification.__table__.update()
            .where(Notification.id.in_(unread_notification_ids))
            .where(Notification.scope == "user")
            .where(Notification.user_id == user.id)
            .values(read_at=now)
        )

    await db.commit()
    return {"marked_read": marked_read}


@router.delete("/{notification_id}")
async def delete_notification(
    notification_id: uuid.UUID,
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss a notification for the current user."""
    notification, _read_at = await _get_visible_notification(
        notification_id, user=user, db=db,
    )
    now = datetime.now(timezone.utc)
    stmt = _read_upsert().values(
        notification_id=notification_id,
        user_id=user.id,
        read_at=None,
        dismissed_at=now,
    ).on_conflict_do_update(
        index_elements=["notification_id", "user_id"],
        set_={"dismissed_at": now},
    )
    await db.execute(stmt)
    if notification.scope == "user" and notification.user_id == user.id and notification.read_at is None:
        notification.read_at = now
    await db.commit()
    return {"deleted": True}


@router.delete("")
async def delete_all_notifications(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Dismiss all visible notifications for the current user."""
    query, read_row = await _visible_query(user, db)
    visible_ids = query.with_only_columns(Notification.id).subquery()
    deleted = await db.scalar(select(func.count()).select_from(visible_ids)) or 0
    if deleted:
        now = datetime.now(timezone.utc)
        if _engine_dialect_name() == "postgresql":
            stmt = _read_upsert().from_select(
                ["notification_id", "user_id", "read_at", "dismissed_at"],
                select(
                    visible_ids.c.id,
                    literal(user.id),
                    literal(None),
                    literal(now),
                ),
            ).on_conflict_do_update(
                index_elements=["notification_id", "user_id"],
                set_={"dismissed_at": now},
            )
            await db.execute(stmt)
        else:
            values = [
                {
                    "notification_id": notification_id,
                    "user_id": user.id,
                    "read_at": None,
                    "dismissed_at": now,
                }
                for notification_id in (
                    await db.scalars(select(visible_ids.c.id))
                ).all()
            ]
            if values:
                await db.execute(
                    sqlite_insert(NotificationRead)
                    .values(values)
                    .on_conflict_do_nothing(
                        index_elements=["notification_id", "user_id"]
                    )
                )
                await db.execute(
                    NotificationRead.__table__.update()
                    .where(NotificationRead.user_id == user.id)
                    .where(
                        NotificationRead.notification_id.in_(
                            [value["notification_id"] for value in values]
                        )
                    )
                    .values(dismissed_at=now)
                )
    await db.commit()
    return {"deleted": deleted}
