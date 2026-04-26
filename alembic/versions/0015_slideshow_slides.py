"""slideshow asset type + slideshow_slides table

A SLIDESHOW is a synthetic Asset whose content is an ordered sequence of
existing IMAGE/VIDEO source assets resolved on the device.  This migration
adds the new ``SLIDESHOW`` value to the ``assettype`` enum and creates the
``slideshow_slides`` child table.

Notes:

* Postgres enum values match SQLAlchemy enum *names* (uppercase) — the
  baseline migration created ``Enum('VIDEO', 'IMAGE', ...)`` and that's
  how the column is persisted.  We add ``'SLIDESHOW'`` to match.
* On SQLite (used by the test fixture's ``Base.metadata.create_all``)
  this migration is never run; the new enum value is included
  automatically when the table is created.  The block below short-
  circuits for SQLite so ``alembic upgrade head`` against a SQLite URL
  remains a no-op for the enum step.
* ``ALTER TYPE ... ADD VALUE`` is allowed inside a transaction on
  Postgres 12+, which we already require.

Revision ID: 0015
Revises: 0014
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    if is_postgres:
        op.execute("ALTER TYPE assettype ADD VALUE IF NOT EXISTS 'SLIDESHOW'")

    op.create_table(
        "slideshow_slides",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("slideshow_asset_id", sa.UUID(), nullable=False),
        sa.Column("source_asset_id", sa.UUID(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("duration_ms", sa.Integer(), nullable=False),
        sa.Column(
            "play_to_end",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["slideshow_asset_id"], ["assets.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["source_asset_id"], ["assets.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "slideshow_asset_id", "position", name="uq_slideshow_slide_position"
        ),
        sa.CheckConstraint("duration_ms > 0", name="ck_slideshow_slide_duration_pos"),
        sa.CheckConstraint("position >= 0", name="ck_slideshow_slide_position_nonneg"),
    )
    op.create_index(
        "ix_slideshow_slides_slideshow_asset_id",
        "slideshow_slides",
        ["slideshow_asset_id"],
    )
    op.create_index(
        "ix_slideshow_slides_source_asset_id",
        "slideshow_slides",
        ["source_asset_id"],
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0015 is not supported")
