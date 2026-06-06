"""Alembic migration 0040 — bind composed-editor chat threads to an asset.

Adds nullable ``composed_asset_id`` to ``chat_threads``.  A
``composed_editor``-mode thread (see migration 0039) is created
server-side by the composed-slide editor and bound to the single
composed-slide asset it edits.  The agent forces this asset id onto
the composed asset-scoped tools (``get_composed_layout`` /
``set_composed_widgets``) so the editor assistant can only ever touch
*this* slide's draft.

``ondelete=CASCADE``: editor chats are asset-scoped ephemera, so
deleting the underlying composed slide deletes its editor chat too.
General-mode threads leave the column NULL.

Revision ID: 0040
Revises: 0039
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0040"
down_revision = "0039"
branch_labels = None
depends_on = None


def _uuid_type(bind):
    """UUID column type matching the dialect (Postgres in prod, SQLite in tests)."""
    if bind.dialect.name == "postgresql":
        return sa.dialects.postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "chat_threads",
        sa.Column("composed_asset_id", _uuid_type(bind), nullable=True),
    )
    op.create_index(
        "ix_chat_threads_composed_asset_id",
        "chat_threads",
        ["composed_asset_id"],
    )
    # SQLite (test harness) can't ALTER TABLE ADD CONSTRAINT; the FK is a
    # Postgres-only concern.  Skipping it under SQLite is safe — the ORM
    # still declares the relationship and tests don't rely on DB-level
    # cascade.
    if bind.dialect.name == "postgresql":
        op.create_foreign_key(
            "fk_chat_threads_composed_asset_id_assets",
            "chat_threads",
            "assets",
            ["composed_asset_id"],
            ["id"],
            ondelete="CASCADE",
        )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0040 is not supported")
