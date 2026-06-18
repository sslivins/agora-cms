"""Alembic migration 0046 — hybrid tag timeline (agora-cms#806 successor).

Phase 0 of the hybrid tag-timeline redesign: a slideshow's deck becomes a
single ordered list of ``slideshow_slides`` rows where each row is either a
static ``asset`` slide (a specific source asset — the classic slide) or a
dynamic ``tag`` block that expands in-place at resolve time to every
non-deleted asset currently carrying ``tag_id``.

This migration is **purely additive and backward-compatible**.  It does NOT
migrate the legacy 1:1 ``slideshow_tag_rules`` rows (0045) into tag-kind
slides, and does NOT drop that table — both happen in Phase 1.  Existing
manual decks keep working unchanged: the new ``kind`` column defaults to
``asset`` so every existing row is a static slide, and ``source_asset_id``
stays populated.

Changes:

* ``assets.slideshow_anchor_at`` (nullable timestamptz) — the persisted,
  set-once-never-re-floored cycle anchor for a slideshow that contains a
  tag block, so growing the block leaves on-screen offsets stable.
  Slideshow-only meaning, like ``assets.shuffle``.
* ``slideshow_slides.kind`` (VARCHAR(8) NOT NULL DEFAULT 'asset') — the
  slide kind discriminator (``asset`` | ``tag``).
* ``slideshow_slides.tag_id`` (nullable UUID FK → tags.id ON DELETE
  CASCADE) — the tag whose membership makes up a ``tag`` block.
* ``slideshow_slides.tag_order_by`` (nullable VARCHAR(32)) — member
  ordering within a tag block (``tagged_at`` in v1).
* ``slideshow_slides.source_asset_id`` relaxed to NULLABLE (a ``tag``
  block pins a tag, not a source asset).
* Two CHECK constraints (kind value + kind/columns invariant) and an index
  on ``tag_id``.

The test harness builds the schema via ``Base.metadata.create_all`` (not
these migrations), so SQLite-incompatible DDL (``ALTER TABLE ... ADD
CONSTRAINT``) is fine here — this path only runs against Postgres.

Revision ID: 0046
Revises: 0045
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0046"
down_revision = "0045"
branch_labels = None
depends_on = None


def _uuid_type(bind):
    """UUID column type matching the dialect (Postgres in prod)."""
    if bind.dialect.name == "postgresql":
        return sa.dialects.postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()
    uuid_t = _uuid_type(bind)

    # --- assets: per-slideshow persisted cycle anchor ---------------------
    op.add_column(
        "assets",
        sa.Column("slideshow_anchor_at", sa.DateTime(timezone=True), nullable=True),
    )

    # --- slideshow_slides: kind discriminator + tag-block columns ---------
    op.add_column(
        "slideshow_slides",
        sa.Column(
            "kind",
            sa.String(length=8),
            nullable=False,
            server_default="asset",
        ),
    )
    op.add_column(
        "slideshow_slides",
        sa.Column("tag_id", uuid_t, nullable=True),
    )
    op.add_column(
        "slideshow_slides",
        sa.Column("tag_order_by", sa.String(length=32), nullable=True),
    )
    op.create_foreign_key(
        "fk_slideshow_slides_tag",
        "slideshow_slides",
        "tags",
        ["tag_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # A tag block pins a tag, not a source asset, so source_asset_id must
    # become nullable.  Every existing row is kind='asset' (the new
    # default) and keeps its source_asset_id, so this widening is safe.
    op.alter_column(
        "slideshow_slides",
        "source_asset_id",
        existing_type=uuid_t,
        nullable=True,
    )

    op.create_check_constraint(
        "ck_slideshow_slide_kind_known",
        "slideshow_slides",
        "kind IN ('asset','tag')",
    )
    op.create_check_constraint(
        "ck_slideshow_slide_kind_columns",
        "slideshow_slides",
        "(kind = 'asset' AND source_asset_id IS NOT NULL AND tag_id IS NULL) "
        "OR (kind = 'tag' AND tag_id IS NOT NULL AND source_asset_id IS NULL)",
    )
    op.create_index(
        "ix_slideshow_slides_tag_id",
        "slideshow_slides",
        ["tag_id"],
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0046 is not supported")
