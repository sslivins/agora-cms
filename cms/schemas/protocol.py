"""WebSocket protocol message types — shared contract with device repo (sslivins/agora).

Protocol version: 1

Any changes to this file MUST be mirrored in the device-side implementation.
"""

from datetime import datetime
from enum import Enum
from typing import Optional

from pydantic import BaseModel

PROTOCOL_VERSION = 1


# ── Base ──


class MessageType(str, Enum):
    # Device → CMS
    REGISTER = "register"
    STATUS = "status"
    ASSET_ACK = "asset_ack"
    ASSET_DELETED = "asset_deleted"
    FETCH_REQUEST = "fetch_request"
    FETCH_FAILED = "fetch_failed"

    # CMS → Device
    SYNC = "sync"
    PLAY = "play"
    STOP = "stop"
    FETCH_ASSET = "fetch_asset"
    DELETE_ASSET = "delete_asset"
    CONFIG = "config"
    AUTH_ASSIGNED = "auth_assigned"
    REBOOT = "reboot"
    UPGRADE = "upgrade"
    FACTORY_RESET = "factory_reset"
    WIPE_ASSETS = "wipe_assets"
    REQUEST_LOGS = "request_logs"

    # Device → CMS (response)
    LOGS_RESPONSE = "logs_response"
    WIPE_ASSETS_ACK = "wipe_assets_ack"


class BaseMessage(BaseModel):
    type: MessageType
    protocol_version: int = PROTOCOL_VERSION


# ── Device → CMS ──


class RegisterMessage(BaseMessage):
    type: MessageType = MessageType.REGISTER
    device_id: str
    auth_token: str
    firmware_version: str
    device_name: Optional[str] = None
    device_name_custom: bool = False
    device_type: str = ""
    storage_capacity_mb: int
    storage_used_mb: int


class StatusMessage(BaseMessage):
    type: MessageType = MessageType.STATUS
    device_id: str
    mode: str  # "play", "stop", "splash"
    asset: Optional[str] = None
    pipeline_state: str = "NULL"
    started_at: Optional[str] = None
    playback_position_ms: Optional[int] = None
    uptime_seconds: int = 0
    storage_used_mb: int = 0
    cpu_temp_c: Optional[float] = None
    error: Optional[str] = None
    error_timestamp: Optional[str] = None
    ssh_enabled: Optional[bool] = None
    local_api_enabled: Optional[bool] = None
    display_connected: Optional[bool] = None


class AssetAckMessage(BaseMessage):
    type: MessageType = MessageType.ASSET_ACK
    device_id: str
    asset_name: str
    checksum: str


class AssetDeletedMessage(BaseMessage):
    type: MessageType = MessageType.ASSET_DELETED
    device_id: str
    asset_name: str


# ── CMS → Device ──


class ScheduleEntry(BaseModel):
    """A single schedule rule pushed to the device."""
    id: str
    name: str
    asset: str
    asset_checksum: Optional[str] = None  # SHA-256 of the file the device should have
    start_time: str          # "HH:MM"
    end_time: str            # "HH:MM"
    start_date: Optional[str] = None  # "YYYY-MM-DD" or null (open-ended)
    end_date: Optional[str] = None    # "YYYY-MM-DD" or null (open-ended)
    days_of_week: Optional[list[int]] = None  # ISO 1-7, null = every day
    priority: int = 0
    loop_count: Optional[int] = None  # None = infinite, N = play exactly N times


class SyncMessage(BaseMessage):
    type: MessageType = MessageType.SYNC
    device_status: Optional[str] = None  # "pending", "adopted", "orphaned", etc.
    timezone: str = "UTC"
    schedules: list[ScheduleEntry] = []
    default_asset: Optional[str] = None
    default_asset_checksum: Optional[str] = None
    splash: Optional[str] = None


class PlayMessage(BaseMessage):
    type: MessageType = MessageType.PLAY
    asset: str
    loop: bool = True
    loop_count: Optional[int] = None


class StopMessage(BaseMessage):
    type: MessageType = MessageType.STOP


class FetchAssetMessage(BaseMessage):
    type: MessageType = MessageType.FETCH_ASSET
    asset_name: str
    download_url: str
    checksum: str
    size_bytes: int


class DeleteAssetMessage(BaseMessage):
    type: MessageType = MessageType.DELETE_ASSET
    asset_name: str


class ConfigMessage(BaseMessage):
    type: MessageType = MessageType.CONFIG
    splash: Optional[str] = None
    device_name: Optional[str] = None
    web_password: Optional[str] = None
    api_key: Optional[str] = None
    ssh_enabled: Optional[bool] = None
    local_api_enabled: Optional[bool] = None


class AuthAssignedMessage(BaseMessage):
    type: MessageType = MessageType.AUTH_ASSIGNED
    device_auth_token: str


class RebootMessage(BaseMessage):
    type: MessageType = MessageType.REBOOT


class FactoryResetMessage(BaseMessage):
    type: MessageType = MessageType.FACTORY_RESET


class WipeAssetsMessage(BaseMessage):
    type: MessageType = MessageType.WIPE_ASSETS
    reason: str = ""  # "adopted", "deleted" — informational for device logs


class UpgradeMessage(BaseMessage):
    type: MessageType = MessageType.UPGRADE


class RequestLogsMessage(BaseMessage):
    type: MessageType = MessageType.REQUEST_LOGS
    request_id: str
    services: Optional[list[str]] = None  # e.g. ["agora-player", "agora-api"]; None = all
    since: str = "24h"  # journalctl --since format, e.g. "24h", "1h", "2026-04-08"


class LogsResponseMessage(BaseMessage):
    type: MessageType = MessageType.LOGS_RESPONSE
    request_id: str
    device_id: str
    logs: dict[str, str] = {}  # service_name -> log text
    error: Optional[str] = None
