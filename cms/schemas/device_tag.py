"""Pydantic schemas for the group-scoped device tag API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


class DeviceTagIn(BaseModel):
    name: str
    color: Optional[str] = None


class DeviceTagPatch(BaseModel):
    name: Optional[str] = None
    color: Optional[str] = None


class DeviceTagOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    group_id: uuid.UUID
    name: str
    color: str
    created_at: datetime
    group_name: Optional[str] = None
    # ``<Group>:<Tag>`` — the scheduler's target identity. Tags are scoped to
    # their group, so the bare name is not globally meaningful.
    qualified_name: Optional[str] = None
    device_count: Optional[int] = None


class DeviceTagsUpdate(BaseModel):
    """Replace the full tag set of one device."""

    tag_ids: list[uuid.UUID] = []


class DeviceTagsOut(BaseModel):
    device_id: str
    group_id: Optional[uuid.UUID] = None
    tags: list[DeviceTagOut] = []
