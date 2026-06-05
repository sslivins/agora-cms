"""Alembic migration 0039 — composed-editor mode on chat threads.

Adds ``mode`` to ``chat_threads`` so a thread can select which tool
profile the assistant runs with.  ``"general"`` (the default and the
only value any existing thread has) keeps the full read + approval-gated
fleet tools; ``"composed_editor"`` exposes only the small composed-slide
editor tool profile (see
:mod:`cms.services.assistant.mcp_client` for ``tools_for_mode``).

The column is NOT NULL with a ``"general"`` server default so every
pre-existing row backfills to general mode automatically.  Editor-mode
threads are created server-side by the composed-slide editor (a later
PR); the public chat-thread create endpoint never sets a non-general
mode, so a client cannot self-select the editor profile.

Revision ID: 0039
Revises: 0038
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0039"
down_revision = "0038"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chat_threads",
        sa.Column(
            "mode",
            sa.String(length=32),
            nullable=False,
            server_default="general",
        ),
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0039 is not supported")
