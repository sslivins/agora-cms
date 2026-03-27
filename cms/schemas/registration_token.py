"""Pydantic schemas for registration tokens."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field


class RegistrationTokenCreate(BaseModel):
    label: str = ""
    max_uses: int = Field(default=1, ge=1)
    expires_at: Optional[datetime] = None


class RegistrationTokenOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    token: str
    label: str
    max_uses: int
    use_count: int
    is_active: bool
    created_at: datetime
    expires_at: Optional[datetime] = None
