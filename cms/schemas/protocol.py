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

    # CMS → Device
    SYNC = "sync"
    PLAY = "play"
    STOP = "stop"
    FETCH_ASSET = "fetch_asset"
    DELETE_ASSET = "delete_asset"
    CONFIG = "config"


class BaseMessage(BaseModel):
    type: MessageType
    protocol_version: int = PROTOCOL_VERSION


# ── Device → CMS ──


class RegisterMessage(BaseMessage):
    type: MessageType = MessageType.REGISTER
    device_id: str
    auth_token: str
    firmware_version: str
    storage_capacity_mb: int
    storage_used_mb: int


class StatusMessage(BaseMessage):
    type: MessageType = MessageType.STATUS
    device_id: str
    mode: str  # "play", "stop", "splash"
    asset: Optional[str] = None
    uptime_seconds: int = 0
    storage_used_mb: int = 0


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


class ScheduleWindow(BaseModel):
    asset: str
    start: datetime
    end: datetime
    loop: bool = True
    priority: int = 0


class SyncMessage(BaseMessage):
    type: MessageType = MessageType.SYNC
    current: Optional[ScheduleWindow] = None
    next: Optional[ScheduleWindow] = None
    default_asset: Optional[str] = None
    splash: Optional[str] = None


class PlayMessage(BaseMessage):
    type: MessageType = MessageType.PLAY
    asset: str
    loop: bool = True


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
