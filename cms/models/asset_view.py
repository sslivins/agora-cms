"""AssetView ORM model for per-user saved asset-library filter presets.

Phase 3 of Asset Library enhancements. Each row stores a named bundle
of filter state (q, type, group_id, uploader_id, tag_id, usage, date
range, sort order, view mode) so a user can recall a frequently-used
view from a dropdown without re-applying chips every visit.

Views are user-private. Setting ``is_default=True`` on one view
atomically clears it on all the user's other views via a transaction
the API issues before commit; the partial unique index just guarantees
that invariant at the DB level.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy import text as sqlite_text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from cms.database import Base


# Use JSONB on Postgres, generic JSON on SQLite (test matrix).
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class AssetView(Base):
    __tablename__ = "asset_views"
    __table_args__ = (
        UniqueConstraint("user_id", "name", name="uq_asset_view_user_name"),
        # Only one default per user. Created as a partial unique index in
        # Postgres; SQLite ignores the WHERE clause but the application
        # layer enforces the same invariant.
        Index(
            "uq_asset_view_user_default",
            "user_id",
            unique=True,
            postgresql_where="is_default",
            sqlite_where=sqlite_text("is_default = 1"),
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
    )
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    filters: Mapped[dict] = mapped_column(_JSON_TYPE, nullable=False, default=dict)
    is_default: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
    )
