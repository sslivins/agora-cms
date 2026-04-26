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
