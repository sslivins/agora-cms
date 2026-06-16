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

# ``composed_siblings_v1`` indicates the device's os_updater pre-fetches
# the source assets (videos / images) embedded in a Composed Slide bundle
# before swapping the bundle into the active cache.  When the CMS sends a
# ``FETCH_ASSET`` for a COMPOSED asset and the device advertises this
# capability, the message carries an extra ``siblings`` list of resolved
# per-device variants for every asset referenced by the bundle.  Older
# firmware never sees ``siblings`` and just renders the bundle on top of
# whatever the local /assets cache already contains.  Pairs with agora
# firmware PR #253 (APT 1.11.95).
CAPABILITY_COMPOSED_SIBLINGS_V1 = "composed_siblings_v1"

# ``slideshow_composed_v1`` indicates the device can render a COMPOSED
# slide *as a member of a slideshow* — i.e. a slide whose
# ``SlideDescriptor.asset_type`` is ``"composed"``, delivered with the
# bundle HTML as ``download_url`` and the bundle's referenced source
# assets in the per-slide ``siblings`` list.  The slideshow player loop
# routes such a slide to ``show_html(bundle)`` (iframe) instead of
# show_image/show_video.  The CMS only includes composed members in a
# slideshow manifest sent to a device advertising this capability (see
# the capability gate in :mod:`cms.services.slideshow_resolver`); older
# firmware never receives a composed slide and so never chokes on the
# unknown type.  Pairs with the agora firmware PR that lights up the
# slideshow-loop composed branch.
CAPABILITY_SLIDESHOW_COMPOSED_V1 = "slideshow_composed_v1"

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
#   "1.2" — adds COMPOSED slide members: a ``SlideDescriptor`` may now
#           carry ``asset_type="composed"`` with ``download_url`` set to
#           the published bundle HTML and an optional per-slide
#           ``siblings`` list (resolved source-asset dependencies of the
#           bundle, same shape as the standalone-composed
#           ``FetchAssetMessage.siblings``).  Gated by the
#           ``slideshow_composed_v1`` capability — a slideshow containing
#           composed members is only sent to devices that advertise it,
#           so a 1.0/1.1 player never sees an unknown slide type.
#   "1.3" — adds optional per-slide ``fit`` ("cover"/"contain") and
#           ``effect`` ("none"/"ken_burns") on ``SlideDescriptor``.
#           ``fit`` controls object-fit; ``effect`` enables a slow
#           pan/zoom (Ken Burns) animation.  Devices that don't
#           understand 1.3 ignore the extras and fall back to the
#           previous cover/no-animation behaviour.
#   "1.4" — adds optional per-slide ``effect_direction``
#           ("in"/"out"/"left"/"right"/"up"/"down") on
#           ``SlideDescriptor`` (the Ken Burns pan/zoom direction;
#           only meaningful when ``effect == "ken_burns"``; default
#           "in" reproduces the 1.3 zoom-in), plus deck-level
#           ``shuffle`` (bool) + ``shuffle_seed`` (stable int) on
#           ``FetchAssetMessage``.  When ``shuffle`` is set the device
#           plays a deterministic per-cycle shuffle of the slide order
#           seeded by ``(shuffle_seed, cycle_index)``.  Devices that
#           don't understand 1.4 ignore both and play the authored
#           order with the default zoom-in.
#
# Rule for future bumps: minor bumps are additive (old players ignore
# unknown fields).  A breaking change bumps the *major* and is gated
# via a new capability string (mirrors ``CAPABILITY_SLIDESHOW_V1``).
SLIDESHOW_MANIFEST_SCHEMA_VERSION_LATEST = "1.4"
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
    # Total cycle length in milliseconds (Phase 1b of agora#226).  Sum of
    # ``slide.duration_ms`` across the deck.  Optional / additive; v1.0
    # devices ignore it.  Emitted whenever ``slides`` is non-empty.  NOT
    # part of the manifest content hash — it's a pure function of slide
    # durations which are already hashed.
    cycle_duration_ms: Optional[int] = None
    # Wall-clock anchor for the slideshow cycle, ISO-8601 UTC string
    # (e.g. "2026-05-23T18:30:00Z").  Phase 1b: present ONLY for ad-hoc
    # / default-asset plays where the CMS picks a cycle boundary aligned
    # to "now".  For *scheduled* plays the device computes the anchor
    # from the active ``ScheduleEntry.start_time`` (wall-clock of the
    # currently-active window) so a reboot mid-window resyncs without
    # the CMS having to tell each device a different start time.
    # Optional / additive; v1.0 devices ignore it.
    started_at: Optional[str] = None
    # Composed-slide sibling dependencies (Phase 1D of agora#253).  Only
    # present (non-None) when ``asset_type`` is ``composed`` AND the device
    # advertises ``composed_siblings_v1``.  Each entry describes one
    # source asset (video / image / saved_stream) embedded in the bundle
    # so the device's os_updater can pre-fetch it into the local
    # /assets/<dir>/<filename> cache before swapping the bundle in.  Empty
    # / None means "no siblings declared" (e.g. a composed slide that is
    # all text + clock widgets, or a draft with no referenced assets).
    siblings: Optional[list["Sibling"]] = None
    # Deck-level shuffle (manifest schema 1.4).  Only present (non-None)
    # when ``asset_type`` is ``slideshow``.  When ``shuffle`` is true the
    # device plays a deterministic per-cycle shuffle of the authored slide
    # order: for cycle index N (``elapsed_ms // cycle_duration_ms``) it
    # derives a permutation seeded by ``(shuffle_seed, N)``, so every
    # device in the fleet shows the same order for the same cycle and a
    # reboot resyncs mid-cycle.  ``shuffle_seed`` is a stable integer
    # derived from the asset id (identical across re-fetches) so it does
    # NOT perturb the resolved manifest checksum; only the ``shuffle`` bool
    # is folded into the checksum so toggling it re-pushes.  Optional /
    # additive — devices that don't understand 1.4 ignore both and play
    # the authored order.
    shuffle: Optional[bool] = None
    shuffle_seed: Optional[int] = None


class SlideDescriptor(BaseModel):
    """One slide in a resolved slideshow manifest, sent inside FetchAssetMessage."""

    asset_name: str
    asset_type: str  # "image", "video", or "composed"
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
    # Per-slide display effects (manifest schema 1.3).  Optional on the
    # wire so v1.0–1.2 device parsers ignore them.  ``fit`` controls
    # object-fit ("cover"/"contain"); ``effect`` enables a slow pan/zoom
    # ("none"/"ken_burns").  Defaults match the pre-1.3 behaviour.
    fit: str = "cover"
    effect: str = "none"
    # Ken Burns pan/zoom direction (manifest schema 1.4).  Optional on the
    # wire so v1.0–1.3 device parsers ignore it.  Only meaningful when
    # ``effect == "ken_burns"``; default "in" reproduces the 1.3 zoom-in.
    effect_direction: str = "in"
    # Composed-slide sibling dependencies (manifest schema 1.2).  Only
    # present (non-None) when ``asset_type`` is ``"composed"``: the
    # resolved source assets (video / image / saved_stream) embedded in
    # the bundle so the device can pre-fetch them into the local cache
    # before showing the bundle, exactly like a standalone composed
    # asset's ``FetchAssetMessage.siblings``.  Empty / None means the
    # composed slide has no media siblings (all-text/clock bundle).
    siblings: Optional[list["Sibling"]] = None

    @model_validator(mode="after")
    def _validate_invariants(self) -> "SlideDescriptor":
        if self.asset_type not in ("image", "video", "composed"):
            raise ValueError(
                "SlideDescriptor.asset_type must be 'image', 'video' or "
                f"'composed', got {self.asset_type!r}"
            )
        if self.duration_ms <= 0:
            raise ValueError(
                f"SlideDescriptor.duration_ms must be positive, got {self.duration_ms}"
            )
        if self.play_to_end and self.asset_type != "video":
            raise ValueError(
                "SlideDescriptor.play_to_end=True is only valid for video sources"
            )
        if self.siblings is not None and self.asset_type != "composed":
            raise ValueError(
                "SlideDescriptor.siblings is only valid for composed slides"
            )
        # Keep this allow-list in sync with cms.schemas.asset.SLIDE_TRANSITIONS
        # and the JS shell's KNOWN_TRANSITIONS in agora/player/shell/player.js.
        from cms.schemas.asset import SLIDE_TRANSITIONS
        if self.transition not in SLIDE_TRANSITIONS:
            raise ValueError(
                f"SlideDescriptor.transition must be one of "
                f"{SLIDE_TRANSITIONS}, got {self.transition!r}"
            )
        if self.transition_ms < 0 or self.transition_ms > 5000:
            raise ValueError(
                f"SlideDescriptor.transition_ms must be in [0, 5000], "
                f"got {self.transition_ms}"
            )
        # Keep these allow-lists in sync with cms.schemas.asset.SLIDE_FITS /
        # SLIDE_EFFECTS and the JS shell's player allow-lists.
        from cms.schemas.asset import SLIDE_EFFECTS, SLIDE_FITS
        if self.fit not in SLIDE_FITS:
            raise ValueError(
                f"SlideDescriptor.fit must be one of {SLIDE_FITS}, "
                f"got {self.fit!r}"
            )
        if self.effect not in SLIDE_EFFECTS:
            raise ValueError(
                f"SlideDescriptor.effect must be one of {SLIDE_EFFECTS}, "
                f"got {self.effect!r}"
            )
        # Keep this allow-list in sync with
        # cms.schemas.asset.KEN_BURNS_DIRECTIONS and the JS shell's player
        # allow-list.
        from cms.schemas.asset import KEN_BURNS_DIRECTIONS
        if self.effect_direction not in KEN_BURNS_DIRECTIONS:
            raise ValueError(
                f"SlideDescriptor.effect_direction must be one of "
                f"{KEN_BURNS_DIRECTIONS}, got {self.effect_direction!r}"
            )
        return self


class Sibling(BaseModel):
    """One source-asset dependency of a Composed Slide bundle.

    The device uses ``name`` as the on-disk filename under
    ``/opt/agora/assets/<videos|images>/`` (matching the ``src``
    attributes baked into the bundle HTML by
    :mod:`cms.composed.publish`), and downloads from ``download_url``
    verifying against ``checksum`` and ``size_bytes``.  When the source
    asset has a profile-matched READY :class:`AssetVariant`, the CMS
    populates the variant's URL / checksum / size here so the device
    receives an already-transcoded file; ``name`` always stays as the
    source asset filename so it lines up with the bundle's HTML refs.
    """

    name: str
    asset_type: str  # "video" | "image" | "saved_stream"
    download_url: str
    checksum: str
    size_bytes: int

    @model_validator(mode="after")
    def _validate_invariants(self) -> "Sibling":
        if self.asset_type not in ("video", "image", "saved_stream"):
            raise ValueError(
                "Sibling.asset_type must be one of "
                "('video', 'image', 'saved_stream'), got "
                f"{self.asset_type!r}"
            )
        # ``name`` lands on the device filesystem under /opt/agora/assets/<dir>/<name>;
        # reject path-traversal shapes outright so a compromised CMS row
        # (or future migration bug) can't escape the cache directory.
        if not self.name or self.name in (".", "..") or "/" in self.name or "\\" in self.name:
            raise ValueError(
                f"Sibling.name must be a bare filename (no path separators); got {self.name!r}"
            )
        if self.size_bytes < 0:
            raise ValueError(
                f"Sibling.size_bytes must be non-negative, got {self.size_bytes}"
            )
        return self


# Resolve forward references now that ``SlideDescriptor`` and ``Sibling``
# are defined.
SlideDescriptor.model_rebuild()
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
