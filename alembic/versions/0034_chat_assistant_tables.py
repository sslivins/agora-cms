"""Alembic migration 0034 — chat assistant tables (PR 2 of 6).

Adds the two tables used by the Phase-1 Assistant data plane:

* ``chat_threads``  — per-user conversation envelopes.
* ``chat_messages`` — one row per turn (user / assistant / tool / system).

Follow-up migrations land the approval queue (``chat_pending_approvals``)
and the per-user budget (``chat_user_budget``) with the PRs that
actually use them, so the table-vs-feature blast radius stays small.

Revision ID: 0034
Revises: 0033
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0034"
down_revision = "0033"
branch_labels = None
depends_on = None


def _types(bind):
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
    return uuid_type, json_type


def upgrade() -> None:
    bind = op.get_bind()
    uuid_type, json_type = _types(bind)

    op.create_table(
        "chat_threads",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column(
            "user_id",
            uuid_type,
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(length=200), nullable=False, server_default=""),
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
    )
    op.create_index(
        "ix_chat_threads_user_id", "chat_threads", ["user_id"], unique=False
    )
    op.create_index(
        "ix_chat_threads_updated_at",
        "chat_threads",
        ["updated_at"],
        unique=False,
    )

    op.create_table(
        "chat_messages",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column(
            "thread_id",
            uuid_type,
            sa.ForeignKey("chat_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(length=20), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("tool_calls", json_type, nullable=True),
        sa.Column("tool_call_id", sa.String(length=100), nullable=True),
        sa.Column("tokens_in", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("tokens_out", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_messages_thread_id",
        "chat_messages",
        ["thread_id"],
        unique=False,
    )
    op.create_index(
        "ix_chat_messages_created_at",
        "chat_messages",
        ["created_at"],
        unique=False,
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0034 is not supported")
