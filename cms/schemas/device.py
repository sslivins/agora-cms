"""Pydantic schemas for device API."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from cms.models.device import DeviceStatus


class DeviceOut(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    status: DeviceStatus
    group_id: Optional[uuid.UUID] = None
    group_name: Optional[str] = None
    default_asset_id: Optional[uuid.UUID] = None
    firmware_version: str
    device_type: str = ""
    storage_capacity_mb: int
    storage_used_mb: int
    last_seen: Optional[datetime] = None
    registered_at: datetime


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    status: Optional[DeviceStatus] = None
    group_id: Optional[uuid.UUID] = None
    default_asset_id: Optional[uuid.UUID] = None


class DeviceGroupOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    description: str
    default_asset_id: Optional[uuid.UUID] = None
    device_count: int = 0
    created_at: datetime


class DeviceGroupCreate(BaseModel):
    name: str
    description: str = ""
    default_asset_id: Optional[uuid.UUID] = None


class DeviceGroupUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    default_asset_id: Optional[uuid.UUID] = None
