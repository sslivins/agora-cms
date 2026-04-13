"""Lightweight audit logging helper.

Usage in a router:
    from cms.services.audit_service import audit_log
    await audit_log(db, user=current_user, action="device.reboot",
                    resource_type="device", resource_id=str(device_id),
                    details={"reason": "manual"}, request=request)
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from cms.models.audit_log import AuditLog

if TYPE_CHECKING:
    from fastapi import Request
    from sqlalchemy.ext.asyncio import AsyncSession
    from cms.models.user import User


async def audit_log(
    db: AsyncSession,
    *,
    user: User | None = None,
    action: str,
    resource_type: str = "",
    resource_id: str | None = None,
    details: dict | None = None,
    request: Request | None = None,
) -> AuditLog:
    """Insert an audit log row and flush (caller owns the commit)."""
    ip = None
    if request and request.client:
        ip = request.client.host

    entry = AuditLog(
        user_id=user.id if user else None,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        details=details,
        ip_address=ip,
    )
    db.add(entry)
    await db.flush()
    return entry
