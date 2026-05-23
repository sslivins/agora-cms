"""WebSocket protocol message types — shared contract with device repo (sslivins/agora).

Protocol version: 2

Any changes to this file MUST be mirrored in the device-side implementation.
"""

from datetime import datetime
from enum import Enum
import re
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Protocol versioning
#
# ``PROTOCOL_VERSION`` is the canonical current version.  ``SUPPORTED_PROTOCOL_VERSIONS``
# is the set the CMS accepts on the REGISTER handshake.  Keep older versions
# here during fleet OTA rollouts so devices running previous firmware don't
# get kicked when the CMS bumps its version.
#
#   v1 — original text-only JSON protocol.
#   v2 — adds binary ``LGCK`` frames carrying chunked ``LOGS_RESPONSE``
#        payloads so log bundles larger than the 1 MiB WPS message cap
#        can be delivered over the device WebSocket.  Small payloads
#        still use the original JSON ``LOGS_RESPONSE`` path.
PROTOCOL_VERSION = 2
SUPPORTED_PROTOCOL_VERSIONS = frozenset({1, 2})

# Capability strings advertised by the device in the REGISTER handshake
# (see ``RegisterMessage.capabilities``).  Used by the CMS to gate features
# that require specific firmware behaviour.  ``slideshow_v1`` indicates the
# device can render a slideshow asset whose slides are inlined in a
# ``FETCH_ASSET`` message.  Older firmware advertises no capabilities and
# is gated out of slideshow scheduling / default-asset assignment.
CAPABILITY_SLIDESHOW_V1 = "slideshow_v1"

# Slideshow manifest schema version (semver, additive minor bumps).  The
# wire format of the slideshow manifest is independent of the
# higher-level ``PROTOCOL_VERSION``: protocol bumps describe the WS
# message envelope and binary frame encoding, schema bumps describe
# which optional fields a ``FetchAssetMessage`` slideshow payload
# (and its persisted on-device JSON) carries.
#
#   "1.0" — historical implicit version. ``slides`` only; no extras.
#           A FetchAssetMessage with no ``manifest_schema_version``
#           field is implicitly 1.0.
#   "1.1" — adds optional ``cycle_duration_ms`` (sibling of ``slides``),
#           optional ``started_at`` (wall-clock anchor, only present
#           for ad-hoc / default-asset plays), and per-slide
#           ``transition``/``transition_ms`` on ``SlideDescriptor``.
#           Devices that don't understand 1.1 ignore the extras and
#           keep playing the deck with the legacy relative-timer chain.
#           Not yet emitted by the CMS — Phase 1a/1b lights it up.
#
# Rule for future bumps: minor bumps are additive (old players ignore
# unknown fields).  A breaking change bumps the *major* and is gated
# via a new capability string (mirrors ``CAPABILITY_SLIDESHOW_V1``).
SLIDESHOW_MANIFEST_SCHEMA_VERSION_LATEST = "1.0"
SLIDESHOW_MANIFEST_SCHEMA_VERSION_DEFAULT = "1.0"

# Binary-frame magic for chunked log responses (Stage 3c of #345).  Pi
# firmware advertising the ``logs_chunk_v1`` capability sends these as
# WS binary frames when a log bundle exceeds the single-message cap.
LOGS_CHUNK_MAGIC = b"LGCK"
LOGS_CHUNK_HEADER_VERSION = 1


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
    OS_UPDATE_DISPATCH = "os_update_dispatch"
    FACTORY_RESET = "factory_reset"
    WIPE_ASSETS = "wipe_assets"
    REQUEST_LOGS = "request_logs"
    # Device → CMS: per-OTA lifecycle event.  See
    # ``cms.services.device_inbound`` for the ``WIRE_TO_CMS_EVENT`` map
    # and ``cms.services.ota_progress`` for how the wire payload turns
    # into UI-visible badge state.
    LIFECYCLE_EVENT = "lifecycle_event"

    # Device → CMS (playback events)
    PLAYBACK_STARTED = "playback_started"
    PLAYBACK_ENDED = "playback_ended"

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
    # Firmware-advertised feature flags.  Older firmware omits this field
    # and the CMS treats it as an empty list.  See ``CAPABILITY_*`` above.
    capabilities: list[str] = Field(default_factory=list)


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
    # Per-HDMI-port connection state (issue #350).  Multi-port boards
    # report one entry per port; ``display_connected`` mirrors port 0
    # for backward compat with single-port readers.  Older firmware
    # omits this field entirely.
    display_ports: Optional[list["PortStatus"]] = None


class PortStatus(BaseModel):
    """One HDMI port's connection state — element of ``display_ports``."""

    name: str
    connected: bool


# ``StatusMessage.display_ports`` is declared with a forward reference
# above; resolve it now that ``PortStatus`` is defined.
StatusMessage.model_rebuild()


class AssetAckMessage(BaseMessage):
    type: MessageType = MessageType.ASSET_ACK
    device_id: str
    asset_name: str
    checksum: str


class AssetDeletedMessage(BaseMessage):
    type: MessageType = MessageType.ASSET_DELETED
    device_id: str
    asset_name: str


class PlaybackStartedMessage(BaseMessage):
    type: MessageType = MessageType.PLAYBACK_STARTED
    device_id: str
    schedule_id: str
    schedule_name: str
    asset: str
    timestamp: str  # ISO 8601 UTC — when the device started playback


class PlaybackEndedMessage(BaseMessage):
    type: MessageType = MessageType.PLAYBACK_ENDED
    device_id: str
    schedule_id: str
    schedule_name: str
    asset: str
    timestamp: str  # ISO 8601 UTC — when the device ended playback


# ── CMS → Device ──


class ScheduleEntry(BaseModel):
    """A single schedule rule pushed to the device."""
    id: str
    name: str
    asset: str
    asset_checksum: Optional[str] = None  # SHA-256 of the file the device should have
    asset_type: Optional[str] = None  # "video", "image", "webpage" — helps device choose playback mode
    url: Optional[str] = None  # URL to render (webpage assets only)
    start_time: str          # "HH:MM:SS"
    end_time: str            # "HH:MM:SS"
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
    asset_type: Optional[str] = None  # video, image, saved_stream, slideshow — helps device route to correct dir
    # Slideshow manifest.  Only present (non-None) when ``asset_type`` is
    # ``slideshow``: an ordered list of resolved source slides the device
    # should fetch and play in sequence.  ``download_url`` and
    # ``size_bytes`` on the outer message are empty/zero for slideshows
    # (no top-level file); ``checksum`` is the resolved manifest content
    # hash (hash of structural metadata + per-slide variant checksums)
    # so the device can short-circuit when nothing has changed.  Older
    # firmware without ``slideshow_v1`` capability never receives a
    # slideshow FETCH_ASSET (capability gate in the scheduler /
    # default-asset endpoints prevents slideshows being assigned to
    # incompatible devices in the first place).
    slides: Optional[list["SlideDescriptor"]] = None
    # Schema version of the slideshow manifest carried in ``slides`` and
    # the persisted on-device JSON.  Optional for backward compatibility
    # with the unversioned v1.0 shape — a missing field is treated as
    # ``"1.0"`` by both sides.  Bumped in Phase 1b to ``"1.1"`` to
    # advertise wall-clock-anchor support.  See
    # ``SLIDESHOW_MANIFEST_SCHEMA_VERSION_*`` constants at the top of
    # this module for the version table.
    manifest_schema_version: Optional[str] = None


class SlideDescriptor(BaseModel):
    """One slide in a resolved slideshow manifest, sent inside FetchAssetMessage."""

    asset_name: str
    asset_type: str  # "image" or "video"
    download_url: str
    checksum: str
    size_bytes: int
    duration_ms: int
    play_to_end: bool = False
    # Per-slide transition controls (Phase 1a of agora#226).  Optional on
    # the wire so a v1.0 device parser ignores them; a v1.1+ player reads
    # them.  ``transition`` is one of cut/fade/dissolve/wipe.  Default
    # behaviour (``cut`` / 600 ms) matches the pre-versioning era.
    transition: str = "cut"
    transition_ms: int = 600

    @model_validator(mode="after")
    def _validate_invariants(self) -> "SlideDescriptor":
        if self.asset_type not in ("image", "video"):
            raise ValueError(
                f"SlideDescriptor.asset_type must be 'image' or 'video', got {self.asset_type!r}"
            )
        if self.duration_ms <= 0:
            raise ValueError(
                f"SlideDescriptor.duration_ms must be positive, got {self.duration_ms}"
            )
        if self.play_to_end and self.asset_type != "video":
            raise ValueError(
                "SlideDescriptor.play_to_end=True is only valid for video sources"
            )
        if self.transition not in ("cut", "fade", "dissolve", "wipe"):
            raise ValueError(
                f"SlideDescriptor.transition must be one of "
                f"cut/fade/dissolve/wipe, got {self.transition!r}"
            )
        if self.transition_ms < 0 or self.transition_ms > 5000:
            raise ValueError(
                f"SlideDescriptor.transition_ms must be in [0, 5000], "
                f"got {self.transition_ms}"
            )
        return self


# Resolve forward reference now that ``SlideDescriptor`` is defined.
FetchAssetMessage.model_rebuild()


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


# ── os_update_dispatch (CMS → Device, agora-os bundle OTA) ──
#
# Wire schema is mirrored from sslivins/agora at ``os_updater/dispatch.py``;
# the device-side ``DispatchPayload`` is vendored in this repo at
# ``tests/contract/device_dispatch_validator.py`` and the contract test in
# ``tests/test_schemas.py`` round-trips a CMS-built message through it to catch
# drift. The two regex strings below MUST match the device-side strings
# byte-for-byte. See plan.md §"Phase M3" for context.

_OS_UPDATE_DISPATCH_VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[A-Za-z0-9.]+)?$")
_OS_UPDATE_DISPATCH_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")


class OSUpdateDispatchMessage(BaseMessage):
    """OS-bundle dispatch (CMS → Device).

    Sent from the CMS Upgrade button to a device's WPS connection. Consumed
    by ``agora-os-updater`` on-device, which strips ``type`` + ``protocol_version``
    before validating the payload against its own ``DispatchPayload`` schema
    (see ``tests/contract/device_dispatch_validator.py`` for the vendored copy).
    """

    model_config = ConfigDict(extra="ignore")

    type: MessageType = MessageType.OS_UPDATE_DISPATCH

    release_id: str
    target_version: str
    min_from_version: str
    bundle_url: str
    signature_url: str
    force_now: bool = False
    force_downgrade: bool = False
    not_before: Optional[str] = None

    @field_validator("release_id")
    @classmethod
    def _check_release_id(cls, value: str) -> str:
        if not _OS_UPDATE_DISPATCH_RELEASE_ID_RE.match(value):
            raise ValueError(
                "release_id must match [A-Za-z0-9._-]{1,128}"
            )
        return value

    @field_validator("target_version", "min_from_version")
    @classmethod
    def _check_version(cls, value: str) -> str:
        if not _OS_UPDATE_DISPATCH_VERSION_RE.match(value):
            raise ValueError(
                "version must be major.minor.patch (with optional -prerelease)"
            )
        return value

    @field_validator("bundle_url", "signature_url")
    @classmethod
    def _check_url(cls, value: str) -> str:
        if not (value.startswith("https://") or value.startswith("http://")):
            raise ValueError("url must use http(s) scheme")
        return value


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
