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
    os_version: str = ""
    device_type: str = ""
    supported_codecs: str = ""
    storage_capacity_mb: int
    storage_used_mb: int
    last_seen: Optional[datetime] = None
    registered_at: datetime
    is_online: bool = False
    is_upgrading: bool = False
    # Issue agora-cms#626 -- True iff the device has been in the
    # ``tryboot`` phase for more than ``STUCK_TRYBOOT_TTL`` (15 min)
    # without progress.  In that state the on-device os-updater FSM
    # is wedged in ``tryboot_running`` and will silently drop every
    # upgrade dispatch.  The UI uses this flag to (a) render a "Upgrade
    # stalled" warning badge, (b) disable the Update kebab item with an
    # explanatory tooltip, and (c) short-circuit ``upgradeDevice()``
    # with a toast.  The upgrade endpoint also refuses 409 server-side.
    upgrade_stuck: bool = False
    # ── OTA progress (issue agora-cms#574) ──
    # While a device is in the middle of an OS OTA, these fields drive
    # the live progress badge in the UI.  All None when no OTA is in
    # flight, OR when the row is stale (last update older than
    # ``OTA_FRESH_TTL`` per ``cms.routers.devices._ota_fields_for_out``).
    # The UI falls back to the legacy "Upgrading…" badge when
    # ``is_upgrading`` is True but these are all None — that's the
    # transition window between the upgrade claim landing and the first
    # lifecycle event arriving, plus any time a firmware older than
    # ``agora#215`` is mid-OTA.
    ota_phase: Optional[str] = None
    ota_label: Optional[str] = None
    ota_pct: Optional[float] = None
    ota_bytes_done: Optional[int] = None
    ota_bytes_total: Optional[int] = None
    playback_mode: Optional[str] = None
    playback_asset: Optional[str] = None
    pipeline_state: Optional[str] = None
    display_connected: Optional[bool] = None
    # Per-HDMI-port state — see issue #350.  ``None`` for older firmware
    # or single-port boards that don't surface per-port detail.
    display_ports: Optional[list[dict]] = None
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



