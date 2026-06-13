"""Pydantic schemas for asset API."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from cms.models.asset import AssetType, VariantStatus
from cms.schemas.tag import TagOut


# Maximum number of slides allowed per slideshow.  Mirrored in the API
# router for input validation; defined here so tests + UI code can import
# the same constant.
MAX_SLIDESHOW_SLIDES = 50

# Per-slide duration bounds (milliseconds).  Lower bound rejects unit
# mistakes / runaway-fast cycling; upper bound (1 hour) is well past any
# reasonable slide length and prevents a single slide starving a schedule.
MIN_SLIDE_DURATION_MS = 500
MAX_SLIDE_DURATION_MS = 60 * 60 * 1000
# Default per-slide duration when a client omits ``duration_ms``.  The
# manual builder UI always sends an explicit value, but the AI assistant
# (and the MCP ``set_slideshow_slides`` tool doc + assistant prompt) both
# advertise this field as optional with a 7000 ms default.  Keeping the
# schema default in sync with that contract means an assistant-built slide
# that omits the field no longer 400s.
DEFAULT_SLIDE_DURATION_MS = 7000

# Per-slide transition controls (Phase 1a of agora#226, expanded in 0029).
# ``cut`` is an instant swap (no transition) and is the only mode the
# legacy mpv-based player renders — it ignores everything else.  The
# chromium-player shell renders the rest.  Wire IDs are kept short
# (snake_case) so they round-trip through DB CHECK + JSON cleanly.
SLIDE_TRANSITIONS = (
    "cut",
    "fade",
    "fade_black",
    "dissolve",
    "push",
    "wipe",
    "zoom",
)
DEFAULT_SLIDE_TRANSITION = "cut"
# Transition duration bounds.  ``0`` is valid (and required for ``cut``);
# upper bound is a soft sanity limit — anything longer would dominate the
# slide duration and look broken.
MIN_SLIDE_TRANSITION_MS = 0
MAX_SLIDE_TRANSITION_MS = 5000
DEFAULT_SLIDE_TRANSITION_MS = 600


class AssetVariantOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    profile_id: uuid.UUID
    profile_name: str = ""
    filename: str
    size_bytes: int
    checksum: str
    status: VariantStatus
    progress: float
    error_message: str = ""
    created_at: datetime
    completed_at: Optional[datetime] = None


class AssetUsageRef(BaseModel):
    """One scheduled-or-slideshow reference to an asset."""

    id: uuid.UUID
    name: str


class AssetUsage(BaseModel):
    """Where an asset is referenced.

    ``schedules`` and ``slides`` are capped lists; the ``extra_*`` counts
    surface "and N more" overflow so a hover tooltip can render
    "Schedule A, Schedule B, +3 more" without paying for every name in
    every list payload.
    """

    schedules: list[AssetUsageRef] = Field(default_factory=list)
    slides: list[AssetUsageRef] = Field(default_factory=list)
    extra_schedules: int = 0
    extra_slides: int = 0
    total: int = 0


class AssetOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    filename: str
    original_filename: Optional[str] = None
    display_name: Optional[str] = None
    asset_type: AssetType
    size_bytes: int
    checksum: str
    duration_seconds: Optional[float] = None
    uploaded_at: datetime
    is_global: bool = False
    uploaded_by_user_id: Optional[uuid.UUID] = None
    url: Optional[str] = None
    thumbnail_url: Optional[str] = None
    capture_duration: Optional[int] = None
    tags: list[TagOut] = Field(default_factory=list)
    usage: Optional[AssetUsage] = None
    # True for a composed slide that has never been published (no rendered
    # bundle / checksum yet). Drives the "UNPUBLISHED" badge in the asset
    # library so users see — before scheduling — that the slide isn't live.
    unpublished: bool = False


class AssetPageOut(BaseModel):
    """Paginated asset listing for the asset library UI.

    Used by ``GET /api/assets/page``. The legacy ``GET /api/assets`` still
    returns the flat ``List[AssetOut]`` and is unchanged.
    """

    items: list[AssetOut]
    next_cursor: Optional[str] = None
    total_estimate: int


BULK_ACTIONS = ("delete", "add_group", "remove_group", "set_global", "add_tag", "remove_tag")


class AssetBulkIn(BaseModel):
    """Request body for ``POST /api/assets/bulk``."""

    asset_ids: list[uuid.UUID] = Field(..., min_length=1, max_length=500)
    action: str
    group_id: Optional[uuid.UUID] = None
    tag_id: Optional[uuid.UUID] = None
    is_global: Optional[bool] = None

    @field_validator("action")
    @classmethod
    def _validate_action(cls, v: str) -> str:
        if v not in BULK_ACTIONS:
            raise ValueError(f"action must be one of {BULK_ACTIONS}")
        return v


class AssetBulkFailure(BaseModel):
    id: uuid.UUID
    reason: str
    status: int = 400


class AssetBulkOut(BaseModel):
    """Result of ``POST /api/assets/bulk``."""

    succeeded: list[uuid.UUID]
    failed: list[AssetBulkFailure]


# ── Slideshow ──


class SlideIn(BaseModel):
    """One slide in a create/replace slideshow request body."""

    source_asset_id: uuid.UUID
    duration_ms: int = Field(
        DEFAULT_SLIDE_DURATION_MS, ge=MIN_SLIDE_DURATION_MS, le=MAX_SLIDE_DURATION_MS
    )
    play_to_end: bool = False
    # Per-slide transition that runs BEFORE the slide appears (i.e. attached
    # to the slide on the right of a gap).  Optional on the wire — old UI
    # clients that don't send the field land on ``cut`` / ``0`` which is
    # exactly the pre-Phase-1a behaviour.
    transition: str = Field(DEFAULT_SLIDE_TRANSITION)
    transition_ms: int = Field(
        DEFAULT_SLIDE_TRANSITION_MS,
        ge=MIN_SLIDE_TRANSITION_MS,
        le=MAX_SLIDE_TRANSITION_MS,
    )

    @field_validator("transition")
    @classmethod
    def _validate_transition(cls, v: str) -> str:
        if v not in SLIDE_TRANSITIONS:
            raise ValueError(
                f"transition must be one of {SLIDE_TRANSITIONS}, got {v!r}"
            )
        return v


class SlideOut(BaseModel):
    """One slide in a GET /slides response.

    Embeds source-asset metadata the builder UI needs (filename, type,
    duration_seconds) so it doesn't have to round-trip per slide.
    """

    model_config = {"from_attributes": True}

    id: uuid.UUID
    position: int
    duration_ms: int
    play_to_end: bool
    transition: str = DEFAULT_SLIDE_TRANSITION
    transition_ms: int = DEFAULT_SLIDE_TRANSITION_MS
    source_asset_id: uuid.UUID
    source_filename: str
    source_asset_type: AssetType
    source_duration_seconds: Optional[float] = None
    thumbnail_url: Optional[str] = None

