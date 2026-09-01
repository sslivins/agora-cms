"""Pydantic schemas for notifications."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from pydantic import BaseModel


class NotificationOut(BaseModel):
    id: uuid.UUID
    scope: str
    level: str
    title: str
    message: str
    details: dict | None = None
    group_id: uuid.UUID | None = None
    group_ids: list[uuid.UUID] = []
    user_id: uuid.UUID | None = None
    created_at: datetime
    read_at: datetime | None = None

    model_config = {"from_attributes": True}

    @staticmethod
    def _normalize_utc(value: datetime | None) -> datetime | None:
        if value is None:
            return None
        return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)

    @classmethod
    def from_notification(
        cls,
        notification,
        *,
        read_at: datetime | None,
    ) -> "NotificationOut":
        group_ids = list(notification.target_group_ids)
        return cls(
            id=notification.id,
            scope=notification.scope,
            level=notification.level,
            title=notification.title,
            message=notification.message,
            details=notification.details,
            group_id=notification.group_id or (group_ids[0] if group_ids else None),
            group_ids=group_ids,
            user_id=notification.user_id,
            created_at=cls._normalize_utc(notification.created_at),
            read_at=cls._normalize_utc(read_at),
        )


class NotificationCount(BaseModel):
    unread: int
