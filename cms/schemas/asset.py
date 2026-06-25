"""Pydantic schemas for asset API."""

import uuid
from datetime import date, datetime, time
from typing import Optional

from pydantic import BaseModel, Field, field_validator, model_validator

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

# Default per-slide duration for a dynamic *tag block* when a client omits
# ``duration_ms``.  Relocated here from the retired ``slideshow_tag_rule``
# model (the hybrid tag-timeline redesign folded tag rules into ordinary
# ``slideshow_slides`` rows of ``kind='tag'``).  Still consumed by the
# builder UI default + migration 0047.
DEFAULT_TAG_SLIDE_DURATION_MS = 8000

# How members of a dynamic tag block are ordered.  Only ``tagged_at`` is
# supported in v1 (AssetTag.created_at asc — the order that preserves the
# no-restart, append-at-tail guarantee).  Relocated from the retired
# ``slideshow_tag_rule`` model.
SLIDESHOW_TAG_ORDER_BY_VALUES = ("tagged_at",)
DEFAULT_SLIDESHOW_TAG_ORDER_BY = "tagged_at"

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
    description: Optional[str] = None
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
    """One slide in a create/replace slideshow request body.

    A slide is one of two *kinds* (hybrid tag-timeline redesign):

    * ``asset`` (default — and what every pre-redesign client sends, since
      they omit ``kind`` entirely): a static slide pinning a specific
      ``source_asset_id``.
    * ``tag``: a dynamic block pinning a ``tag_id`` that expands in-place
      at resolve time to every non-deleted asset carrying the tag (ordered
      by ``tag_order_by``).  Carries no ``source_asset_id``; its playback
      fields (duration/transition/fit/effect) become the deck-defaults
      every expanded member inherits.
    """

    # Slide-kind discriminator.  Absent ⇒ ``asset`` so old tag-unaware
    # clients round-trip unchanged.
    kind: str = Field("asset")
    # Required for ``asset`` kind, must be absent for ``tag`` kind (enforced
    # by the model validator below).
    source_asset_id: Optional[uuid.UUID] = None
    # Required for ``tag`` kind, must be absent for ``asset`` kind.
    tag_id: Optional[uuid.UUID] = None
    tag_order_by: str = Field(DEFAULT_SLIDESHOW_TAG_ORDER_BY)
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
    # Tag-block member transition (only meaningful for ``tag`` kind):
    # the transition BETWEEN expanded members (members 1..N), distinct
    # from ``transition`` which is the transition INTO the block.  Both
    # optional/nullable — ``None`` means "inherit ``transition``" (the
    # original behaviour), so an old client that never sends them yields
    # byte-identical output.  Ignored / forced None for ``asset`` kind.
    member_transition: Optional[str] = None
    member_transition_ms: Optional[int] = Field(
        None,
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

    # ── Per-slide visibility window (all None = always visible) ──
    #
    # Restrict WHEN this slide is eligible to show.  Evaluated server-side
    # against the requesting device's effective local time; a closed slide
    # is dropped from the resolved deck.  Every field is optional — omitting
    # all five (what every pre-feature client does) means "always visible".
    # These fields apply to BOTH ``asset`` and ``tag`` kinds.
    #
    # Local-calendar date range, INCLUSIVE both ends.
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    # Weekdays the slide may show on, 0=Mon..6=Sun.  None/empty = every day.
    # Normalised (de-duped, sorted, ``[]`` → None) by the validator below.
    active_days: Optional[list[int]] = None
    # Local time-of-day window: ``active_start`` inclusive, ``active_end``
    # exclusive; start > end wraps past midnight (e.g. 22:00..02:00).
    active_start: Optional[time] = None
    active_end: Optional[time] = None

    # ── Per-slide video clip (both None = play whole source) ──
    #
    # Restrict a VIDEO ``asset`` slide to a sub-range of its source.
    # ``clip_start_ms`` is the offset the device seeks to; ``clip_duration_ms``
    # (None = "to the natural end") bounds how long to play from there.  Only
    # meaningful for an ``asset`` slide whose source is a VIDEO — enforced
    # against the resolved source type at the service/resolver layer (the
    # source's asset_type isn't known to this schema).  Omitting both (what
    # every pre-feature client does) means "play the whole source / honour
    # play_to_end", so output is byte-identical.
    clip_start_ms: Optional[int] = Field(None, ge=0)
    clip_duration_ms: Optional[int] = Field(None, ge=MIN_SLIDE_DURATION_MS)
    @classmethod
    def _validate_kind(cls, v: str) -> str:
        if v not in ("asset", "tag"):
            raise ValueError(f"kind must be 'asset' or 'tag', got {v!r}")
        return v

    @field_validator("tag_order_by")
    @classmethod
    def _validate_tag_order_by(cls, v: str) -> str:
        if v not in SLIDESHOW_TAG_ORDER_BY_VALUES:
            raise ValueError(
                f"tag_order_by must be one of {SLIDESHOW_TAG_ORDER_BY_VALUES}, got {v!r}"
            )
        return v

    @field_validator("transition")
    @classmethod
    def _validate_transition(cls, v: str) -> str:
        if v not in SLIDE_TRANSITIONS:
            raise ValueError(
                f"transition must be one of {SLIDE_TRANSITIONS}, got {v!r}"
            )
        return v

    @field_validator("member_transition")
    @classmethod
    def _validate_member_transition(cls, v: Optional[str]) -> Optional[str]:
        if v is not None and v not in SLIDE_TRANSITIONS:
            raise ValueError(
                f"member_transition must be one of {SLIDE_TRANSITIONS}, got {v!r}"
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

    @field_validator("active_days")
    @classmethod
    def _validate_active_days(cls, v: Optional[list[int]]) -> Optional[list[int]]:
        """Normalise the weekday set: de-dupe, sort, ``[]`` → None.

        Each entry must be 0..6 (Mon..Sun).  An empty list is treated as
        "no restriction" (same as None / every day) so a client that clears
        all weekday chips round-trips to the always-visible state rather
        than a slide that can never show.
        """
        if v is None:
            return None
        for d in v:
            if d < 0 or d > 6:
                raise ValueError(
                    f"active_days entries must be 0..6 (Mon..Sun), got {d}"
                )
        uniq = sorted(set(v))
        return uniq or None

    @model_validator(mode="after")
    def _validate_visibility_window(self) -> "SlideIn":
        """Cross-field visibility-window coherence (mirrors DB CHECKs).

        * ``valid_to`` must be >= ``valid_from`` when both set (single-day
          window allowed).
        * ``active_start`` and ``active_end`` must differ when both set
          (equal would be a degenerate empty window; a wrap-around window
          where start > end is fine and means "spans midnight").
        """
        if (
            self.valid_from is not None
            and self.valid_to is not None
            and self.valid_to < self.valid_from
        ):
            raise ValueError("valid_to must be on or after valid_from")
        if (
            self.active_start is not None
            and self.active_end is not None
            and self.active_start == self.active_end
        ):
            raise ValueError("active_start and active_end must differ")
        return self

    @model_validator(mode="after")
    def _validate_kind_columns(self) -> "SlideIn":
        """Enforce the kind/columns invariant mirroring the DB CHECK.

        ``asset`` ⇒ ``source_asset_id`` set, ``tag_id`` absent.
        ``tag``   ⇒ ``tag_id`` set, ``source_asset_id`` absent, and
        ``play_to_end`` not allowed (a dynamic block has no single member
        to play to its natural end).
        """
        if self.kind == "asset":
            if self.source_asset_id is None:
                raise ValueError("asset slide requires source_asset_id")
            if self.tag_id is not None:
                raise ValueError("asset slide must not carry tag_id")
            # Member transition is a tag-block-only concept; drop it for
            # asset slides rather than 422-ing a client that sends a stray
            # value, so the DB never stores a meaningless member_transition.
            self.member_transition = None
            self.member_transition_ms = None
        else:  # tag
            if self.tag_id is None:
                raise ValueError("tag slide requires tag_id")
            if self.source_asset_id is not None:
                raise ValueError("tag slide must not carry source_asset_id")
            if self.play_to_end:
                raise ValueError("tag slide cannot set play_to_end")
            # A dynamic tag block expands to many members; there is no single
            # source to clip, so reject clip fields rather than silently
            # dropping them.
            if self.clip_start_ms is not None or self.clip_duration_ms is not None:
                raise ValueError("tag slide cannot set clip_start_ms/clip_duration_ms")
        return self


class SlideOut(BaseModel):
    """One slide in a GET /slides response.

    Embeds source-asset metadata the builder UI needs (filename, type,
    duration_seconds) so it doesn't have to round-trip per slide.  For a
    dynamic ``tag`` slide the source fields are null and ``tag_id`` /
    ``tag_name`` / ``member_count`` describe the block instead.
    """

    model_config = {"from_attributes": True}

    id: uuid.UUID
    position: int
    kind: str = "asset"
    duration_ms: int
    play_to_end: bool
    transition: str = DEFAULT_SLIDE_TRANSITION
    transition_ms: int = DEFAULT_SLIDE_TRANSITION_MS
    # Tag-block member transition; null for ``asset`` kind and for ``tag``
    # blocks that inherit the block transition between members.
    member_transition: Optional[str] = None
    member_transition_ms: Optional[int] = None
    fit: str = DEFAULT_SLIDE_FIT
    effect: str = DEFAULT_SLIDE_EFFECT
    effect_direction: str = DEFAULT_KEN_BURNS_DIRECTION
    # Per-slide visibility window; all null = always visible.
    valid_from: Optional[date] = None
    valid_to: Optional[date] = None
    active_days: Optional[list[int]] = None
    active_start: Optional[time] = None
    active_end: Optional[time] = None
    # Per-slide video clip; both null = play whole source.
    clip_start_ms: Optional[int] = None
    clip_duration_ms: Optional[int] = None
    source_asset_id: Optional[uuid.UUID] = None
    source_filename: Optional[str] = None
    source_asset_type: Optional[AssetType] = None
    source_duration_seconds: Optional[float] = None
    thumbnail_url: Optional[str] = None
    # Populated for ``tag`` kind; null for ``asset`` kind.
    tag_id: Optional[uuid.UUID] = None
    tag_name: Optional[str] = None
    tag_order_by: Optional[str] = None
    member_count: Optional[int] = None

