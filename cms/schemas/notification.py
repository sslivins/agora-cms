"""Pydantic schemas for notifications."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class NotificationOut(BaseModel):
    id: uuid.UUID
    scope: str
    level: str
    title: str
    message: str
    details: dict | None = None
    group_id: uuid.UUID | None = None
    user_id: uuid.UUID | None = None
    created_at: datetime
    read_at: datetime | None = None

    model_config = {"from_attributes": True}


class NotificationCount(BaseModel):
    unread: int
