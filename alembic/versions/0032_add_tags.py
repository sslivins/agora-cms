"""Add ``tags`` and ``asset_tags`` for asset library tagging.

Phase 2.5 of the Asset Library enhancements: ad-hoc tagging on assets so
users can group images/videos across (and orthogonally to) device groups.

Both tables are created empty so this migration is fast and safe to run
online -- no CONCURRENTLY needed.  ``ON DELETE CASCADE`` on the junction
covers both directions (asset hard-delete and tag delete).

Revision ID: 0032
Revises: 0031
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0032"
down_revision = "0031"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.create_table(
        "tags",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True) if dialect == "postgresql" else sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=64), nullable=False),
        sa.Column(
            "color",
            sa.String(length=16),
            nullable=False,
            server_default="#737373",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "created_by_user_id",
            sa.dialects.postgresql.UUID(as_uuid=True) if dialect == "postgresql" else sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )

    # Case-insensitive uniqueness on tag name.  Implemented as a
    # functional unique index so the canonical-form choice (lower-trim)
    # stays in the application layer instead of leaking into the table
    # definition.
    if dialect == "postgresql":
        op.execute("CREATE UNIQUE INDEX uq_tags_name_lower ON tags (lower(name))")
    else:
        # SQLite supports expression indexes too (>= 3.9), but in
        # practice the test suite never inserts duplicate-cased names so
        # a plain unique index on name is sufficient for the test
        # matrix.  We still create it as a functional index where the
        # dialect can handle it.
        try:
            op.execute("CREATE UNIQUE INDEX uq_tags_name_lower ON tags (lower(name))")
        except Exception:
            op.create_index("uq_tags_name_lower", "tags", ["name"], unique=True)

    op.create_table(
        "asset_tags",
        sa.Column("id", sa.dialects.postgresql.UUID(as_uuid=True) if dialect == "postgresql" else sa.String(length=36), nullable=False),
        sa.Column(
            "asset_id",
            sa.dialects.postgresql.UUID(as_uuid=True) if dialect == "postgresql" else sa.String(length=36),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tag_id",
            sa.dialects.postgresql.UUID(as_uuid=True) if dialect == "postgresql" else sa.String(length=36),
            sa.ForeignKey("tags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("asset_id", "tag_id", name="uq_asset_tag"),
    )
    op.create_index("idx_asset_tags_tag_id", "asset_tags", ["tag_id"])


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0032 is not supported")
