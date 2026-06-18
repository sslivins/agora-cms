"""SlideshowSlide model — one ordered entry inside a SLIDESHOW asset.

A *slideshow* is a synthetic Asset (asset_type=SLIDESHOW) whose content is a
sequence of existing IMAGE/VIDEO source assets, each shown for a per-slide
duration before advancing to the next.  Slides are owned by their parent
slideshow (CASCADE on parent delete) and pin their source assets in place
(RESTRICT — sources can't be deleted while any slideshow references them).
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.database import Base


# Canonical Ken Burns direction grammar — kept in lockstep with
# ``cms.schemas.asset.KEN_BURNS_DIRECTIONS`` (the Pydantic-layer source of
# truth) and the JS shell allow-list in ``agora/player/shell/player.js``.
# Defined locally (rather than imported from the cms app layer) so this
# shared model has no upward dependency on ``cms`` and stays import-cycle
# free.  ZOOM (``in``/``out``) × optional PAN (8 compass dirs), plus legacy
# bare-pan aliases.  The DB CHECK constraint below is derived from this
# tuple so the two can never drift (the original drift caused a 500 on
# saving any diagonal/zoom+pan Ken Burns slide — see migration 0044).
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
_KEN_BURNS_DIRECTION_SQL_LIST = ",".join(f"'{_d}'" for _d in KEN_BURNS_DIRECTIONS)


class SlideshowSlide(Base):
    """One slide inside a slideshow asset."""

    __tablename__ = "slideshow_slides"
    __table_args__ = (
        UniqueConstraint(
            "slideshow_asset_id", "position", name="uq_slideshow_slide_position"
        ),
        CheckConstraint("duration_ms > 0", name="ck_slideshow_slide_duration_pos"),
        CheckConstraint("position >= 0", name="ck_slideshow_slide_position_nonneg"),
        # Transition fields (Phase 1a of agora#226).  The allowed transition
        # set is enforced in the Pydantic input schema; the DB check is the
        # belt-and-braces guard against an out-of-band INSERT.  The
        # transition_ms upper bound (5000 ms) matches MAX_SLIDE_TRANSITION_MS
        # in cms.schemas.asset.
        CheckConstraint(
            "transition IN ('cut','fade','fade_black','dissolve','push','wipe','zoom')",
            name="ck_slideshow_slide_transition_known",
        ),
        CheckConstraint(
            "transition_ms >= 0 AND transition_ms <= 5000",
            name="ck_slideshow_slide_transition_ms_range",
        ),
        # Per-slide display effects (agora#7xx).  ``fit`` controls how the
        # media is scaled into the cell (object-fit); ``effect`` is an
        # optional animated treatment (Ken Burns slow pan/zoom).  Both are
        # validated in the Pydantic schema and re-asserted here as a guard
        # against out-of-band INSERTs.
        CheckConstraint(
            "fit IN ('cover','contain','contain_blur')",
            name="ck_slideshow_slide_fit_known",
        ),
        CheckConstraint(
            "effect IN ('none','ken_burns')",
            name="ck_slideshow_slide_effect_known",
        ),
        # Ken Burns pan/zoom direction (agora#261).  Only meaningful when
        # ``effect == 'ken_burns'``; the default ``in`` reproduces the
        # original zoom-in animation.  Validated in the Pydantic schema and
        # re-asserted here against out-of-band INSERTs.  The token grammar
        # encodes a ZOOM (``in``/``out``) and an optional PAN (8 compass
        # directions incl. diagonals): ``in``/``out`` (pure zoom),
        # ``in_<pan>``/``out_<pan>`` (zoom + pan), and legacy bare-pan
        # aliases (``left`` ... = zoom-in pans).  Kept in lockstep with the
        # canonical ``cms.schemas.asset.KEN_BURNS_DIRECTIONS`` tuple — see
        # migration 0044 for the matching DB constraint.
        CheckConstraint(
            f"effect_direction IN ({_KEN_BURNS_DIRECTION_SQL_LIST})",
            name="ck_slideshow_slide_effect_direction_known",
        ),
        # FK columns are not auto-indexed in Postgres; the source-delete guard
        # and ACL re-check queries scan by source_asset_id.
        Index("ix_slideshow_slides_source_asset_id", "source_asset_id"),
        # Hybrid tag timeline (agora#806 successor).  A slide is one of two
        # kinds: a static ``asset`` entry (a specific source asset — the
        # original slide) or a dynamic ``tag`` entry (a tag that expands
        # in-place at resolve time to every non-deleted asset carrying it).
        CheckConstraint(
            "kind IN ('asset','tag')",
            name="ck_slideshow_slide_kind_known",
        ),
        # Kind/columns invariant: an ``asset`` entry pins a source asset and
        # carries no tag; a ``tag`` entry pins a tag and carries no source
        # asset (its source assets are resolved dynamically from membership).
        CheckConstraint(
            "(kind = 'asset' AND source_asset_id IS NOT NULL AND tag_id IS NULL) "
            "OR (kind = 'tag' AND tag_id IS NOT NULL AND source_asset_id IS NULL)",
            name="ck_slideshow_slide_kind_columns",
        ),
        # FK columns aren't auto-indexed; tag-block expansion + the
        # tag-delete guard scan by tag_id.
        Index("ix_slideshow_slides_tag_id", "tag_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    slideshow_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Slide kind discriminator.  ``asset`` (default) is a static slide
    # pinning ``source_asset_id``; ``tag`` is a dynamic block pinning
    # ``tag_id`` that expands in-place to current tag membership at resolve
    # time, every expanded member inheriting this row's playback columns
    # (duration/transition/fit/effect) as deck-defaults.
    kind: Mapped[str] = mapped_column(
        String(8), nullable=False, default="asset", server_default="asset"
    )
    # For ``asset`` kind: the pinned source media (RESTRICT — can't be
    # deleted while referenced).  NULL for ``tag`` kind, where the source
    # assets are resolved dynamically from tag membership.
    source_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=True,
    )
    # For ``tag`` kind: the tag whose members make up this block.  CASCADE
    # so deleting a tag removes its dynamic blocks from every slideshow.
    # NULL for ``asset`` kind.
    tag_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("tags.id", ondelete="CASCADE"),
        nullable=True,
    )
    # For ``tag`` kind: how members are ordered within the block.  Only
    # ``tagged_at`` (AssetTag.created_at asc) is supported in v1, matching
    # the retired SlideshowTagRule.  NULL/ignored for ``asset`` kind.
    tag_order_by: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    duration_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    play_to_end: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    # Per-slide transition that runs BEFORE this slide appears (i.e. the
    # transition is attached to the slide on the right of a gap).  Stored
    # as a short string for forward-compatibility — the allowed set is
    # validated in the Pydantic input schema and re-asserted at the DB
    # level via a CHECK constraint.  Phase 1a only ``cut`` is rendered by
    # the mpv player; the chromium-player branch renders the rest.
    transition: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cut", server_default="cut"
    )
    transition_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=600, server_default="600"
    )
    # How the media scales into the slide cell.  ``cover`` (default) fills
    # the frame and crops overflow; ``contain`` letterboxes to show the
    # whole image.  Mirrors CSS object-fit.
    fit: Mapped[str] = mapped_column(
        String(16), nullable=False, default="cover", server_default="cover"
    )
    # Optional animated treatment applied while the slide is shown.
    # ``none`` (default) is a static frame; ``ken_burns`` is a slow
    # pan/zoom.  Rendered by the chromium player and the composed-bundle
    # slideshow renderer.
    effect: Mapped[str] = mapped_column(
        String(16), nullable=False, default="none", server_default="none"
    )
    # Ken Burns pan/zoom direction.  Only meaningful when ``effect`` is
    # ``ken_burns``.  ``in`` (default) reproduces the original zoom-in
    # animation; the full grammar is a ZOOM (``in``/``out``) plus an
    # optional PAN (8 compass directions incl. diagonals) — e.g.
    # ``out_up_right``.  Rendered by the chromium player and the
    # composed-bundle slideshow renderer.  Allowed set: ``KEN_BURNS_DIRECTIONS``.
    effect_direction: Mapped[str] = mapped_column(
        String(16), nullable=False, default="in", server_default="in"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Two relationships pointing at the Asset table.  ``slideshow`` is the
    # parent (CASCADE), ``source`` is the referenced media (RESTRICT).
    slideshow: Mapped["Asset"] = relationship(
        foreign_keys=[slideshow_asset_id],
    )
    source: Mapped["Asset"] = relationship(
        foreign_keys=[source_asset_id],
    )
