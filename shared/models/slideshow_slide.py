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
            "transition IN ('cut','fade','dissolve','wipe')",
            name="ck_slideshow_slide_transition_known",
        ),
        CheckConstraint(
            "transition_ms >= 0 AND transition_ms <= 5000",
            name="ck_slideshow_slide_transition_ms_range",
        ),
        # FK columns are not auto-indexed in Postgres; the source-delete guard
        # and ACL re-check queries scan by source_asset_id.
        Index("ix_slideshow_slides_source_asset_id", "source_asset_id"),
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
    source_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="RESTRICT"),
        nullable=False,
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
