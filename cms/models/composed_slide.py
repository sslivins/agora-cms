"""ComposedSlide model — backing data for a Composed Slide asset.

A *composed slide* is a CMS-authored multi-widget canvas (text, image,
clock, ticker, video, ...) that is rendered to a single self-contained
HTML bundle and shipped to devices like any other cacheable asset.

One row in this table is bound 1:1 to an :class:`Asset` row of
:attr:`AssetType.COMPOSED`.  The asset row carries the cache metadata
(``url``, ``size_bytes``, ``checksum``); this row carries the
authoring state: the layout JSON, draft flag, AI prompt history, and
bundle build metadata used to detect when the bundle is stale w.r.t.
its source assets.

See :mod:`cms.composed` for the schema, registry, validator, and
(later) bundle builder.
"""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Integer, Boolean, Text, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from shared.database import Base


# Use JSONB on Postgres, JSON on SQLite (used in unit tests).  The
# SQLAlchemy type-decorator pattern is `JSONB().with_variant(JSON(),
# "sqlite")`, which gives us JSONB perf in prod and a working column
# in the test matrix.
_JSON = JSONB().with_variant(JSON(), "sqlite")

# bundle_source_asset_ids is a Postgres ARRAY(UUID) so we can do
# set-membership queries like "all composed slides that reference
# asset X" cheaply.  On SQLite we fall back to JSON storage of the
# same list; the application code reads it as a Python list either
# way.
_UUID_ARRAY = ARRAY(PG_UUID(as_uuid=True)).with_variant(JSON(), "sqlite")


class ComposedSlide(Base):
    """Authoring + bundle-metadata row for a Composed Slide asset."""

    __tablename__ = "composed_slides"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # One composed_slide per asset, cascade-deleted with the asset.
    asset_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # The full layout document.  Shape is validated by
    # :class:`cms.composed.schema.Layout` (Pydantic) on every write;
    # ``schema_version`` mirrors ``Layout.schema_version`` and is
    # denormalized here so we can filter / migrate rows without
    # parsing every JSON blob.
    layout_json: Mapped[dict] = mapped_column(_JSON, nullable=False)
    schema_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )

    # Draft state — True until the user explicitly publishes.  The
    # bundle builder refuses to publish a draft layout to the asset
    # cache; it can still be live-previewed.
    is_draft: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )

    # Last AI prompt used to (re)generate this layout, kept for
    # debugging / future "rerun with tweak" UX.  NULL for hand-built
    # layouts.  ``last_ai_model`` stores deployment + model + date so
    # we can correlate output quality with a specific AOAI revision.
    last_ai_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_ai_model: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Bundle build metadata.  ``bundle_built_at`` is set whenever the
    # builder writes a fresh HTML bundle to the asset cache;
    # ``bundle_source_asset_ids`` records every referenced asset at
    # build time so the editor can detect "source X changed since the
    # bundle was last built" and prompt for a rebuild.
    bundle_built_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    bundle_source_asset_ids: Mapped[list[uuid.UUID] | None] = mapped_column(
        _UUID_ARRAY, nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )

    asset = relationship("Asset", foreign_keys=[asset_id])
