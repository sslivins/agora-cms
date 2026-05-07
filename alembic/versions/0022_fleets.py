"""Add fleets table — DB-backed fleet identity registry.

Replaces the env-only ``Settings.fleet_register_secrets`` map. Fleet
HMAC secrets and IDs now live in Postgres so they can be managed at
runtime via the imager API.

This migration is **destructive of the env source of truth**: after
deploy, the old ``AGORA_CMS_FLEET_REGISTER_SECRETS`` env var is
ignored. Operators must re-create their fleets via
``POST /api/imager/fleets`` (or pass a list to a future seed CLI).
Pre-built ``.img.xz`` artifacts that bake the OLD HMAC will fail to
register against newly-created fleets unless the operator provides
the same ``secret_b64``; the API allows passing an explicit secret
to support this migration path.

Revision ID: 0022
Revises: 0021
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "fleets",
        sa.Column(
            "id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            primary_key=True,
        ),
        sa.Column("fleet_id", sa.Text(), nullable=False),
        sa.Column("secret_b64", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "created_by",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    # Partial unique index on (fleet_id) for live rows only — soft-deleted
    # rows can share an ID with a freshly-created replacement.
    op.create_index(
        "uq_fleets_fleet_id_active",
        "fleets",
        ["fleet_id"],
        unique=True,
        postgresql_where=sa.text("deleted_at IS NULL"),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "0022_fleets is forward-only. Fleet identities are migrated from "
        "the env-only AGORA_CMS_FLEET_REGISTER_SECRETS map into the "
        "fleets table; a downgrade would silently lose any fleet rows "
        "created via the imager API after upgrade. Restore from a "
        "pre-upgrade DB backup if you need to roll back."
    )
