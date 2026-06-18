"""Pydantic schemas for asset API."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator

from cms.models.asset import AssetType, VariantStatus
from cms.models.slideshow_tag_rule import DEFAULT_TAG_SLIDE_DURATION_MS
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

# Per-slide display effects (agora#7xx, #261).  ``fit`` maps to CSS
# object-fit: ``cover`` fills the cell and crops overflow, ``contain``
# letterboxes to show the whole frame.  ``contain_blur`` is a contained
# foreground over a blurred, zoomed cover backdrop of the same image so
# the letterbox bars are filled rather than black.  ``effect`` is an
# optional animated treatment: ``none`` is a static frame, ``ken_burns``
# is a slow pan/zoom.  Both are rendered by the chromium player and the
# composed-bundle slideshow renderer.  Wire IDs are short snake_case so
# they round-trip through DB CHECK + JSON cleanly.  ``contain_blur`` is
# additive — a pre-blur device parser that doesn't recognise it falls
# back to plain ``contain`` (black bars), which is a graceful downgrade.
SLIDE_FITS = ("cover", "contain", "contain_blur")
DEFAULT_SLIDE_FIT = "cover"
SLIDE_EFFECTS = ("none", "ken_burns")
DEFAULT_SLIDE_EFFECT = "none"

# Per-slide Ken Burns pan/zoom direction (slideshow roadmap, agora#261).
# Only meaningful when ``effect == "ken_burns"``; ignored otherwise.  The
# direction token encodes two orthogonal tracks: a ZOOM (``in`` | ``out``)
# and an optional PAN (one of 8 compass directions, incl. diagonals).  The
# default ``in`` is pure zoom-in and reproduces the original keyframe
# exactly, so every pre-existing ken_burns slide stays byte-identical.
# Wire grammar: ``in``/``out`` (pure zoom), ``in_<pan>``/``out_<pan>``
# (zoom + pan), and legacy bare-pan aliases (``left`` ... render as
# zoom-in pans, matching the device shell's ``kbDirectionClass`` parser).
# Additive: a device that doesn't recognise the field falls back to the
# default ``in`` animation (graceful).  Kept in lockstep with the composed
# media widget (``cms.composed.widgets.media``) + agora player.js/css.
_KEN_BURNS_PANS = (
    "left",
    "right",
    "up",
    "down",
    "up_left",
    "up_right",
    "down_left",
    "down_right",
)
KEN_BURNS_DIRECTIONS = (
    "in",
    "out",
    *(f"in_{_p}" for _p in _KEN_BURNS_PANS),
    *(f"out_{_p}" for _p in _KEN_BURNS_PANS),
    *_KEN_BURNS_PANS,
)
DEFAULT_KEN_BURNS_DIRECTION = "in"


# Filler words that may appear when a human (or an LLM relaying a human
# request) describes a direction, e.g. "zoom out going right-down".
_KEN_BURNS_FILLER_WORDS = frozenset(
    {"zoom", "pan", "and", "going", "go", "to", "the", "then", "motion", "drift"}
)


def normalize_effect_direction(value: str) -> str:
    """Best-effort canonicalisation of a Ken Burns direction token.

    The canonical grammar is ZOOM (``in``/``out``) + an optional PAN, with
    diagonals written vertical-first (``out_down_right``).  Humans — and the
    assistant relaying them — naturally write these in any order and with any
    separator: ``"out-right-down"``, ``"zoom out right down"``,
    ``"right_down"`` all mean ``out_down_right`` (or ``down_right``).

    This collapses separators (``-``/space/``/``/``+`` → ``_``), drops filler
    words, and reorders the pan components so order never matters.  If the
    input can be mapped onto a canonical token it returns that token;
    otherwise it returns ``value`` unchanged so the caller's membership check
    raises the usual descriptive error.
    """
    if not isinstance(value, str):
        return value
    raw = value.strip().lower()
    if raw in KEN_BURNS_DIRECTIONS:
        return raw
    for sep in ("-", " ", "/", "+", ","):
        raw = raw.replace(sep, "_")
    parts = [p for p in raw.split("_") if p and p not in _KEN_BURNS_FILLER_WORDS]

    zoom: str | None = None
    vert: list[str] = []
    horiz: list[str] = []
    for p in parts:
        if p in ("in", "out"):
            if zoom is not None and zoom != p:
                return value  # contradictory zoom (e.g. "in_out")
            zoom = p
        elif p in ("up", "down"):
            if p not in vert:
                vert.append(p)
        elif p in ("left", "right"):
            if p not in horiz:
                horiz.append(p)
        else:
            return value  # unknown token — let the validator reject it

    if len(vert) > 1 or len(horiz) > 1:
        return value  # e.g. "up_down" / "left_right" — not a valid pan
    pan = "_".join(vert + horiz)  # vertical-first, matching the canonical set

    if zoom and pan:
        candidate = f"{zoom}_{pan}"
    elif zoom:
        candidate = zoom
    elif pan:
        candidate = pan  # legacy bare-pan alias (zoom-in pan)
    else:
        return value
    return candidate if candidate in KEN_BURNS_DIRECTIONS else value


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
    # Per-slide display effects.  Optional on the wire — old clients that
    # don't send them land on ``cover`` / ``none`` which is the
    # pre-effects behaviour.
    fit: str = Field(DEFAULT_SLIDE_FIT)
    effect: str = Field(DEFAULT_SLIDE_EFFECT)
    # Ken Burns pan/zoom direction.  Only consulted when ``effect`` is
    # ``ken_burns``; harmless otherwise.  Default ``in`` == the original
    # zoom-in animation, so existing slides don't change.
    effect_direction: str = Field(DEFAULT_KEN_BURNS_DIRECTION)

    @field_validator("transition")
    @classmethod
    def _validate_transition(cls, v: str) -> str:
        if v not in SLIDE_TRANSITIONS:
            raise ValueError(
                f"transition must be one of {SLIDE_TRANSITIONS}, got {v!r}"
            )
        return v

    @field_validator("fit")
    @classmethod
    def _validate_fit(cls, v: str) -> str:
        if v not in SLIDE_FITS:
            raise ValueError(f"fit must be one of {SLIDE_FITS}, got {v!r}")
        return v

    @field_validator("effect")
    @classmethod
    def _validate_effect(cls, v: str) -> str:
        if v not in SLIDE_EFFECTS:
            raise ValueError(f"effect must be one of {SLIDE_EFFECTS}, got {v!r}")
        return v

    @field_validator("effect_direction")
    @classmethod
    def _validate_effect_direction(cls, v: str) -> str:
        v = normalize_effect_direction(v)
        if v not in KEN_BURNS_DIRECTIONS:
            raise ValueError(
                f"effect_direction must be one of {KEN_BURNS_DIRECTIONS}, got {v!r}"
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
    fit: str = DEFAULT_SLIDE_FIT
    effect: str = DEFAULT_SLIDE_EFFECT
    effect_direction: str = DEFAULT_KEN_BURNS_DIRECTION
    source_asset_id: uuid.UUID
    source_filename: str
    source_asset_type: AssetType
    source_duration_seconds: Optional[float] = None
    thumbnail_url: Optional[str] = None


class TagRuleIn(BaseModel):
    """Request body to create/replace a slideshow's tag rule (agora-cms#806).

    Putting a tag rule on a slideshow asset flips it into *tag mode*: the
    deck is resolved live from the set of assets carrying ``tag_id`` rather
    than from hand-authored ``slideshow_slides`` rows.  ``order_by`` is not
    exposed on the wire — v1 is hardcoded to ``tagged_at`` (the only value
    that preserves the no-restart, append-at-tail guarantee).

    The ``default_*`` fields apply uniformly to every resolved member, since
    a tag deck has no per-slide authoring UI.  Their validators mirror
    :class:`SlideIn` exactly so a tag deck behaves like a hand-built one
    with default slides.
    """

    tag_id: uuid.UUID
    default_duration_ms: int = Field(
        DEFAULT_TAG_SLIDE_DURATION_MS,
        ge=MIN_SLIDE_DURATION_MS,
        le=MAX_SLIDE_DURATION_MS,
    )
    default_transition: str = Field(DEFAULT_SLIDE_TRANSITION)
    default_transition_ms: int = Field(
        DEFAULT_SLIDE_TRANSITION_MS,
        ge=MIN_SLIDE_TRANSITION_MS,
        le=MAX_SLIDE_TRANSITION_MS,
    )
    default_fit: str = Field(DEFAULT_SLIDE_FIT)
    default_effect: str = Field(DEFAULT_SLIDE_EFFECT)
    default_effect_direction: str = Field(DEFAULT_KEN_BURNS_DIRECTION)

    @field_validator("default_transition")
    @classmethod
    def _validate_transition(cls, v: str) -> str:
        if v not in SLIDE_TRANSITIONS:
            raise ValueError(
                f"default_transition must be one of {SLIDE_TRANSITIONS}, got {v!r}"
            )
        return v

    @field_validator("default_fit")
    @classmethod
    def _validate_fit(cls, v: str) -> str:
        if v not in SLIDE_FITS:
            raise ValueError(f"default_fit must be one of {SLIDE_FITS}, got {v!r}")
        return v

    @field_validator("default_effect")
    @classmethod
    def _validate_effect(cls, v: str) -> str:
        if v not in SLIDE_EFFECTS:
            raise ValueError(f"default_effect must be one of {SLIDE_EFFECTS}, got {v!r}")
        return v

    @field_validator("default_effect_direction")
    @classmethod
    def _validate_effect_direction(cls, v: str) -> str:
        v = normalize_effect_direction(v)
        if v not in KEN_BURNS_DIRECTIONS:
            raise ValueError(
                f"default_effect_direction must be one of {KEN_BURNS_DIRECTIONS}, got {v!r}"
            )
        return v


class TagRuleOut(BaseModel):
    """A slideshow's tag rule, as returned by GET/PUT ``/{id}/tag-rule``.

    ``member_count`` is the number of assets currently resolved into the
    deck (eligible asset types carrying the tag), surfaced so the builder
    UI can show "N items match" without a second round-trip.
    """

    model_config = {"from_attributes": True}

    slideshow_asset_id: uuid.UUID
    tag_id: uuid.UUID
    tag_name: Optional[str] = None
    order_by: str = "tagged_at"
    default_duration_ms: int = DEFAULT_TAG_SLIDE_DURATION_MS
    default_transition: str = DEFAULT_SLIDE_TRANSITION
    default_transition_ms: int = DEFAULT_SLIDE_TRANSITION_MS
    default_fit: str = DEFAULT_SLIDE_FIT
    default_effect: str = DEFAULT_SLIDE_EFFECT
    default_effect_direction: str = DEFAULT_KEN_BURNS_DIRECTION
    anchor_at: Optional[datetime] = None
    member_count: int = 0

