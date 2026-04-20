"""Audit log API routes — read-only access to system audit trail."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import require_permission
from cms.database import get_db
from cms.models.audit_log import AuditLog
from cms.models.user import User
from cms.permissions import AUDIT_READ
from cms.schemas.audit import AuditLogRead

router = APIRouter(prefix="/api/audit-log")


@router.get("", response_model=list[AuditLogRead])
async def list_audit_logs(
    _user: User = Depends(require_permission(AUDIT_READ)),
    db: AsyncSession = Depends(get_db),
    action: str | None = Query(None, description="Filter by action (e.g. device.reboot)"),
    resource_type: str | None = Query(None, description="Filter by resource type"),
    user_id: uuid.UUID | None = Query(None, description="Filter by acting user"),
    since: datetime | None = Query(None, description="Only entries after this timestamp"),
    until: datetime | None = Query(None, description="Only entries before this timestamp"),
    q: str | None = Query(None, description="Free-text search across description, action, resource_type"),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    from sqlalchemy import or_

    stmt = select(AuditLog).options(selectinload(AuditLog.user))

    if action:
        stmt = stmt.where(AuditLog.action == action)
    if resource_type:
        stmt = stmt.where(AuditLog.resource_type == resource_type)
    if user_id:
        stmt = stmt.where(AuditLog.user_id == user_id)
    if since:
        stmt = stmt.where(AuditLog.created_at >= since)
    if until:
        stmt = stmt.where(AuditLog.created_at <= until)
    if q and q.strip():
        like_q = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                AuditLog.description.ilike(like_q),
                AuditLog.action.ilike(like_q),
                AuditLog.resource_type.ilike(like_q),
                AuditLog.resource_id.ilike(like_q),
            )
        )

    stmt = stmt.order_by(AuditLog.created_at.desc()).offset(offset).limit(limit)
    result = await db.execute(stmt)
    entries = result.scalars().all()

    return [
        AuditLogRead(
            id=e.id,
            user_id=e.user_id,
            username=e.user.username if e.user else None,
            action=e.action,
            description=e.description,
            resource_type=e.resource_type,
            resource_id=e.resource_id,
            details=e.details,
            ip_address=e.ip_address,
            created_at=e.created_at,
        )
        for e in entries
    ]


@router.get("/count")
async def audit_log_count(
    _user: User = Depends(require_permission(AUDIT_READ)),
    db: AsyncSession = Depends(get_db),
    action: str | None = Query(None),
    resource_type: str | None = Query(None),
    user_id: uuid.UUID | None = Query(None),
    since: datetime | None = Query(None),
    until: datetime | None = Query(None),
):
    """Return total count matching filters — useful for pagination."""
    q = select(func.count(AuditLog.id))
    if action:
        q = q.where(AuditLog.action == action)
    if resource_type:
        q = q.where(AuditLog.resource_type == resource_type)
    if user_id:
        q = q.where(AuditLog.user_id == user_id)
    if since:
        q = q.where(AuditLog.created_at >= since)
    if until:
        q = q.where(AuditLog.created_at <= until)

    result = await db.execute(q)
    return {"count": result.scalar()}
