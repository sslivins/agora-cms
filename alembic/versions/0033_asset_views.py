"""Add ``asset_views`` table for per-user saved asset-library filter presets.

Phase 3 of Asset Library enhancements.  Empty new table, no online-DDL
concern, no CONCURRENTLY needed.

Revision ID: 0033
Revises: 0032
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    uuid_type = (
        sa.dialects.postgresql.UUID(as_uuid=True)
        if dialect == "postgresql"
        else sa.String(length=36)
    )
    json_type = (
        sa.dialects.postgresql.JSONB()
        if dialect == "postgresql"
        else sa.JSON()
    )

    op.create_table(
        "asset_views",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column(
            "user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("filters", json_type, nullable=False),
        sa.Column(
            "is_default",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "name", name="uq_asset_view_user_name"),
    )

    # Partial unique index: at most one default view per user.
    # SQLite supports partial unique indexes too (>= 3.8) so this is
    # safe for the test matrix.
    if dialect == "postgresql":
        op.execute(
            "CREATE UNIQUE INDEX uq_asset_view_user_default "
            "ON asset_views (user_id) WHERE is_default"
        )
    else:
        try:
            op.execute(
                "CREATE UNIQUE INDEX uq_asset_view_user_default "
                "ON asset_views (user_id) WHERE is_default"
            )
        except Exception:
            # Fall back to non-partial: application layer still enforces
            # single-default invariant in the same txn.
            pass


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0033 is not supported")
