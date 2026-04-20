"""User notification preferences API — per-user email opt-in for event types."""

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_current_user, get_setting
from cms.database import get_db
from cms.models.notification_pref import UserNotificationPref
from cms.models.user import User
from cms.schemas.notification_pref import NotificationPrefOut, NotificationPrefUpdate

router = APIRouter(prefix="/api/notification-preferences")

# Event types that support email notifications
SUPPORTED_EVENT_TYPES = ["offline", "online", "temp_high", "temp_cleared"]


@router.get("", response_model=list[NotificationPrefOut])
async def get_notification_prefs(
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user's notification preferences.

    Returns one entry per supported event type, creating defaults if needed.
    """
    result = await db.execute(
        select(UserNotificationPref).where(
            UserNotificationPref.user_id == user.id,
        )
    )
    existing = {p.event_type: p for p in result.scalars().all()}

    # Ensure all event types have a row
    for et in SUPPORTED_EVENT_TYPES:
        if et not in existing:
            pref = UserNotificationPref(
                user_id=user.id,
                event_type=et,
                email_enabled=False,
            )
            db.add(pref)
            existing[et] = pref
    await db.commit()

    # Refresh to get IDs
    prefs = []
    for et in SUPPORTED_EVENT_TYPES:
        await db.refresh(existing[et])
        prefs.append(existing[et])

    return prefs


@router.put("", response_model=list[NotificationPrefOut])
async def update_notification_prefs(
    updates: list[NotificationPrefUpdate],
    user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Batch-update the current user's notification preferences."""
    result = await db.execute(
        select(UserNotificationPref).where(
            UserNotificationPref.user_id == user.id,
        )
    )
    existing = {p.event_type: p for p in result.scalars().all()}

    for upd in updates:
        if upd.event_type not in SUPPORTED_EVENT_TYPES:
            continue
        pref = existing.get(upd.event_type)
        if pref is None:
            pref = UserNotificationPref(
                user_id=user.id,
                event_type=upd.event_type,
                email_enabled=upd.email_enabled,
            )
            db.add(pref)
            existing[upd.event_type] = pref
        else:
            pref.email_enabled = upd.email_enabled

    await db.commit()

    prefs = []
    for et in SUPPORTED_EVENT_TYPES:
        if et in existing:
            await db.refresh(existing[et])
            prefs.append(existing[et])
    return prefs


@router.get("/email-status")
async def email_notification_status(
    db: AsyncSession = Depends(get_db),
):
    """Check if email notifications are globally enabled."""
    val = await get_setting(db, "email_notifications_enabled")
    return {"enabled": val == "true"}
