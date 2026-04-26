"""Pydantic schemas for asset API."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field

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
    source_asset_id: uuid.UUID
    source_filename: str
    source_asset_type: AssetType
    source_duration_seconds: Optional[float] = None

