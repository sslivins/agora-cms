"""Pydantic schemas for user notification preferences."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class NotificationPrefOut(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID
    event_type: str
    email_enabled: bool
    created_at: datetime

    model_config = {"from_attributes": True}


class NotificationPrefUpdate(BaseModel):
    event_type: str
    email_enabled: bool
