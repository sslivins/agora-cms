"""Chat assistant: pending write-tool approvals (PR 4 of 6).

When the agent loop sees the LLM propose a write tool (anything not
in :data:`cms.services.assistant.mcp_client.READ_ONLY_TOOLS`) it
inserts a row here in state ``pending`` instead of executing the
tool, then yields an ``approval_request`` SSE event so the UI can
render an Approve/Reject card.

Decisions are terminal — once a row is ``approved`` / ``rejected``
it stays for audit.  Replay of the conversation history after the
fact uses ``result_content`` to reconstruct the tool turn the LLM
saw (or the synthetic "user declined" turn on reject).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from cms.database import Base


# Match the chat_messages convention — JSONB on Postgres, plain JSON
# on SQLite (test matrix).
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


# Terminal status values.  Kept as plain strings (not an enum) so
# Alembic migrations stay portable across SQLite (tests) and Postgres
# (prod) without needing CREATE TYPE / DROP TYPE plumbing.
STATUS_PENDING = "pending"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"
STATUS_EXPIRED = "expired"


class ChatPendingApproval(Base):
    __tablename__ = "chat_pending_approvals"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_threads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    proposed_by_message_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_messages.id", ondelete="SET NULL"),
        nullable=True,
    )
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_call_id: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_arguments: Mapped[dict[str, Any]] = mapped_column(
        _JSON_TYPE, nullable=False
    )
    # Snapshot of human-readable values for any UUID-shaped args we
    # could resolve at write time (see ``approval_display``).  NULL on
    # read-tool turns and on legacy rows written before this column
    # existed; the frontend treats a missing key as "render the raw
    # UUID".  Snapshotting keeps the approval card stable even if a
    # device / asset is renamed between propose and approve.
    display_arguments: Mapped[dict[str, Any] | None] = mapped_column(
        _JSON_TYPE, nullable=True
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=STATUS_PENDING,
        server_default=STATUS_PENDING,
        index=True,
    )
    result_content: Mapped[str | None] = mapped_column(Text(), nullable=True)
    decision_note: Mapped[str | None] = mapped_column(Text(), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
    )
    decided_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
