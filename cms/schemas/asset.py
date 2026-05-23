"""Pydantic schemas for asset API."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from cms.models.asset import AssetType, VariantStatus


# Maximum number of slides allowed per slideshow.  Mirrored in the API
# router for input validation; defined here so tests + UI code can import
# the same constant.
MAX_SLIDESHOW_SLIDES = 50

# Per-slide duration bounds (milliseconds).  Lower bound rejects unit
# mistakes / runaway-fast cycling; upper bound (1 hour) is well past any
# reasonable slide length and prevents a single slide starving a schedule.
MIN_SLIDE_DURATION_MS = 500
MAX_SLIDE_DURATION_MS = 60 * 60 * 1000

# Per-slide transition controls (Phase 1a of agora#226).  ``cut`` means an
# instant swap (no transition) and is the only transition the mpv-based
# player actually renders today — ``fade``/``dissolve``/``wipe`` are
# accepted on the wire so the chromium-player branch can render them
# without a follow-up schema bump.  The mpv player ignores anything other
# than ``cut`` (renders the slide instantly).
SLIDE_TRANSITIONS = ("cut", "fade", "dissolve", "wipe")
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
    capture_duration: Optional[int] = None


# ── Slideshow ──


class SlideIn(BaseModel):
    """One slide in a create/replace slideshow request body."""

    source_asset_id: uuid.UUID
    duration_ms: int = Field(..., ge=MIN_SLIDE_DURATION_MS, le=MAX_SLIDE_DURATION_MS)
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

