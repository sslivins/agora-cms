"""Voice announcement asset type + voice_announcements table.

Adds the new ``VOICE_ANNOUNCEMENT`` asset type and its 1:1 authoring /
generation-state table.

Notes:

* Postgres enum values must match SQLAlchemy enum *names* (uppercase), not
  the Python enum ``.value`` strings. Migration 0037 added lowercase
  ``'composed'`` and immediately broke inserts until 0038 added the correct
  uppercase ``'COMPOSED'`` entry. We add ``'VOICE_ANNOUNCEMENT'`` in the
  correct uppercase form up front to avoid repeating that mistake.
* The ``generation_status`` column reuses the existing Postgres ``jobstatus``
  enum type created in the baseline migration; we reference it with
  ``create_type=False`` so Alembic doesn't try to recreate the type.

Revision ID: 0063
Revises: 0062
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0063"
down_revision = "0062"
branch_labels = None
depends_on = None


def _uuid_type(bind):
    if bind.dialect.name == "postgresql":
        return sa.dialects.postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


def _job_status_enum(bind):
    values = ("PENDING", "PROCESSING", "DONE", "FAILED", "CANCELLED")
    if bind.dialect.name == "postgresql":
        return sa.dialects.postgresql.ENUM(
            *values,
            name="jobstatus",
            create_type=False,
        )
    return sa.Enum(*values, name="jobstatus")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "ALTER TYPE assettype ADD VALUE IF NOT EXISTS 'VOICE_ANNOUNCEMENT'"
        )

    op.create_table(
        "voice_announcements",
        sa.Column("id", _uuid_type(bind), primary_key=True, nullable=False),
        sa.Column(
            "asset_id",
            _uuid_type(bind),
            sa.ForeignKey("assets.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("script_text", sa.Text(), nullable=False),
        sa.Column("voice_name", sa.String(length=128), nullable=False),
        sa.Column("emotion", sa.String(length=64), nullable=True),
        sa.Column(
            "language",
            sa.String(length=16),
            nullable=False,
            server_default="en-US",
        ),
        sa.Column(
            "speech_rate",
            sa.String(length=16),
            nullable=True,
        ),
        sa.Column(
            "generation_status",
            _job_status_enum(bind),
            nullable=False,
            server_default="PENDING",
        ),
        sa.Column("generation_error", sa.Text(), nullable=True),
        sa.Column("last_generated_at", sa.DateTime(timezone=True), nullable=True),
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
        "ix_voice_announcements_asset_id",
        "voice_announcements",
        ["asset_id"],
        unique=True,
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0063 is not supported")
