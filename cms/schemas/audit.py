"""Pydantic schemas for the audit log."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel


class AuditLogRead(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None = None
    username: str | None = None
    action: str
    description: str | None = None
    resource_type: str
    resource_id: str | None = None
    details: dict | None = None
    ip_address: str | None = None
    created_at: datetime

    model_config = {"from_attributes": True}
