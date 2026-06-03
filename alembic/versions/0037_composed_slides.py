"""Alembic migration 0037 — composed_slides table + AssetType.COMPOSED.

Backs the new Composed Slide asset type.  See
:mod:`cms.models.composed_slide` and ``plan.md`` for the design.

The AssetType enum is extended with a new value, ``composed``.  On
Postgres this requires an explicit ``ALTER TYPE``; SQLite stores the
enum as a plain string so no DDL is needed there.

Revision ID: 0037
Revises: 0036
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0037"
down_revision = "0036"
branch_labels = None
depends_on = None


def _json_type(bind):
    if bind.dialect.name == "postgresql":
        return sa.dialects.postgresql.JSONB()
    return sa.JSON()


def _uuid_array_type(bind):
    if bind.dialect.name == "postgresql":
        return sa.dialects.postgresql.ARRAY(
            sa.dialects.postgresql.UUID(as_uuid=True)
        )
    # SQLite has no array; store the list as JSON.
    return sa.JSON()


def _uuid_type(bind):
    if bind.dialect.name == "postgresql":
        return sa.dialects.postgresql.UUID(as_uuid=True)
    # SQLite: 36-char string holds the UUID hex form.
    return sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()

    # ── Extend the AssetType enum (Postgres only) ─────────────────
    # ALTER TYPE ... ADD VALUE is not transactional in older PG
    # versions; run it on the connection directly so Alembic doesn't
    # wrap it in a transaction it can't honour.
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE assettype ADD VALUE IF NOT EXISTS 'composed'")

    # ── composed_slides table ────────────────────────────────────
    op.create_table(
        "composed_slides",
        sa.Column("id", _uuid_type(bind), primary_key=True, nullable=False),
        sa.Column(
            "asset_id",
            _uuid_type(bind),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("layout_json", _json_type(bind), nullable=False),
        sa.Column(
            "schema_version",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
        sa.Column(
            "is_draft",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("last_ai_prompt", sa.Text(), nullable=True),
        sa.Column("last_ai_model", sa.Text(), nullable=True),
        sa.Column("bundle_built_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bundle_source_asset_ids", _uuid_array_type(bind), nullable=True),
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
    )
    op.create_index(
        "ix_composed_slides_asset_id",
        "composed_slides",
        ["asset_id"],
        unique=True,
    )


def downgrade() -> None:
    # Postgres can't drop a single value from an enum, and we're not
    # going to ship a downgrade path that orphans composed assets.
    # Drop the table only; the unused enum value is harmless.
    raise NotImplementedError("downgrade of 0037 is not supported")
