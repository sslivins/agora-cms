"""Alembic migration 0035 — chat pending approvals table (PR 4 of 6).

Adds the queue the assistant uses to gate **write** MCP tools.  When
the LLM proposes a write tool (i.e. anything *not* in
:data:`cms.services.assistant.mcp_client.READ_ONLY_TOOLS`) the agent
loop inserts a row here in state ``pending`` and emits an
``approval_request`` event to the user instead of calling MCP.  The
user then approves or rejects via the chat router; on approve the
agent loop persists the tool result and the conversation continues on
the user's next turn.  On reject we synthesise a tool-result message
that tells the LLM the user declined.

Row lifecycle:

* ``pending``   — awaiting user decision (default state).
* ``approved``  — user clicked Approve; row stays for audit.
* ``rejected``  — user clicked Reject; row stays for audit.
* ``expired``   — TTL elapsed before any decision (job-driven; not
                  enforced in PR 4, but the column reserves the state).

Revision ID: 0035
Revises: 0034
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0035"
down_revision = "0034"
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
        "chat_pending_approvals",
        sa.Column("id", uuid_type, nullable=False),
        sa.Column(
            "thread_id",
            uuid_type,
            sa.ForeignKey("chat_threads.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # The assistant turn (assistant role, tool_calls set) that
        # proposed this write; nullable because the row is created
        # before that assistant row is committed on some paths.
        sa.Column(
            "proposed_by_message_id",
            uuid_type,
            sa.ForeignKey("chat_messages.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("tool_call_id", sa.String(length=100), nullable=False),
        sa.Column("tool_arguments", json_type, nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="pending",
        ),
        # The MCP tool result captured on approve (for audit / replay);
        # null until the approve endpoint runs the tool.
        sa.Column("result_content", sa.Text(), nullable=True),
        # Free-form decision context (e.g. "rejected via UI",
        # "auto-approved by admin allowlist") — optional.
        sa.Column("decision_note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "decided_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chat_pending_approvals_thread_id",
        "chat_pending_approvals",
        ["thread_id"],
        unique=False,
    )
    op.create_index(
        "ix_chat_pending_approvals_status",
        "chat_pending_approvals",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0035 is not supported")
