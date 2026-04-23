"""Pydantic schemas for device API."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, StrictBool

from cms.models.device import DeviceStatus


class DeviceOut(BaseModel):
    model_config = {"from_attributes": True}

    id: str
    name: str
    location: str = ""
    status: DeviceStatus
    group_id: Optional[uuid.UUID] = None
    group_name: Optional[str] = None
    default_asset_id: Optional[uuid.UUID] = None
    timezone: Optional[str] = None
    firmware_version: str
    device_type: str = ""
    supported_codecs: str = ""
    storage_capacity_mb: int
    storage_used_mb: int
    last_seen: Optional[datetime] = None
    registered_at: datetime
    is_online: bool = False
    is_upgrading: bool = False
    playback_mode: Optional[str] = None
    playback_asset: Optional[str] = None
    pipeline_state: Optional[str] = None
    display_connected: Optional[bool] = None
    has_active_schedule: bool = False
    # Live state fields from device_manager
    cpu_temp_c: Optional[float] = None
    ip_address: Optional[str] = None
    ssh_enabled: Optional[bool] = None
    local_api_enabled: Optional[bool] = None
    error: Optional[str] = None
    update_available: bool = False
    uptime_seconds: int = 0


class DeviceUpdate(BaseModel):
    name: Optional[str] = None
    location: Optional[str] = None
    status: Optional[DeviceStatus] = None
    group_id: Optional[uuid.UUID] = None
    default_asset_id: Optional[uuid.UUID] = None
    profile_id: Optional[uuid.UUID] = None
    timezone: Optional[str] = None


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


class AdoptRequest(BaseModel):
    name: Optional[str] = None
    location: Optional[str] = None
    group_id: Optional[uuid.UUID] = None
    profile_id: uuid.UUID


class SetPasswordRequest(BaseModel):
    password: str


class ToggleRequest(BaseModel):
    enabled: StrictBool



