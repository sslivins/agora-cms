"""Pydantic schemas for device events."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class DeviceEventOut(BaseModel):
    id: uuid.UUID
    device_id: str | None = None
    device_name: str
    group_id: uuid.UUID | None = None
    group_name: str = ""
    event_type: str
    details: dict | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
