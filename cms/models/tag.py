"""Tag + AssetTag ORM models for Asset Library tagging (Phase 2.5).

Tags are CMS-only metadata used by the asset library UI for ad-hoc grouping.
Workers don't consume tags, so the models live in ``cms.models`` (not
``shared.models``).

Uniqueness on ``name`` is enforced case-insensitively via a functional
unique index on ``lower(name)``.  The application also lower-trims the
input before insertion, so the column itself stores the canonical form.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint, column, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base


# Default chip color (Tailwind neutral-500).  Kept here so the schema
# default and the UI seed match.
DEFAULT_TAG_COLOR = "#737373"


class Tag(Base):
    __tablename__ = "tags"
    __table_args__ = (
        # Case-insensitive uniqueness across the visible tag set.
        Index("uq_tags_name_lower", func.lower(column("name")), unique=True),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[str] = mapped_column(
        String(16), nullable=False, default=DEFAULT_TAG_COLOR, server_default=DEFAULT_TAG_COLOR
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )


class AssetTag(Base):
    """Junction table: assets <-> tags."""

    __tablename__ = "asset_tags"
    __table_args__ = (
        UniqueConstraint("asset_id", "tag_id", name="uq_asset_tag"),
        # Covers the "find all assets with tag X" query.
        Index("idx_asset_tags_tag_id", "tag_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("tags.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
    )
