"""Alembic migration 0038 — add ``COMPOSED`` to the assettype enum.

Migration 0037 added ``'composed'`` (lowercase) to the Postgres
``assettype`` enum, but the column is persisted using SQLAlchemy enum
*names* (uppercase) — see the note at the top of migration 0015 for
the convention.  The lowercase value never matches what SQLAlchemy
sends, so every ``Asset(asset_type=AssetType.COMPOSED)`` insert fails
with ``invalid input value for enum assettype: "COMPOSED"``.

This migration adds the correctly-cased ``'COMPOSED'`` value.  The
unused lowercase ``'composed'`` value from 0037 is left in place
because Postgres can't drop a single enum value without rebuilding
the type, and it's harmless.

SQLite ignores enum constraints, so no-op there (matches 0015 /
0037).

Revision ID: 0038
Revises: 0037
"""

from __future__ import annotations

from alembic import op


revision = "0038"
down_revision = "0037"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TYPE assettype ADD VALUE IF NOT EXISTS 'COMPOSED'")


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0038 is not supported")
