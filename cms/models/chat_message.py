"""Chat assistant: a single message in a thread.

One row per LLM turn participant:
* ``role="user"``    — message typed by the human; ``content`` is text.
* ``role="assistant"`` — model reply.  ``content`` is the final assembled
  text; ``tool_calls`` (JSONB) carries the OpenAI tool-call list when
  the model wanted to invoke an MCP tool that turn.
* ``role="tool"``    — result of an MCP tool invocation. ``tool_call_id``
  joins back to the assistant turn that requested it; ``content`` holds
  the JSON-serialised result.
* ``role="system"``  — only used for ad-hoc system reminders injected by
  the agent loop (e.g. budget warnings).  The base system prompt is
  rebuilt every turn and is NOT persisted.

``tokens_in`` / ``tokens_out`` are recorded on assistant turns from the
LLM usage reply so the budget accounting in ``chat_user_budget`` can be
reconciled per-message without re-parsing the row body.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from cms.database import Base


# Use JSONB on Postgres, generic JSON on SQLite (test matrix).
_JSON_TYPE = JSON().with_variant(JSONB(), "postgresql")


class ChatMessage(Base):
    __tablename__ = "chat_messages"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    thread_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("chat_threads.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role: Mapped[str] = mapped_column(String(20), nullable=False)
    content: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    tool_calls: Mapped[list[dict[str, Any]] | None] = mapped_column(
        _JSON_TYPE, nullable=True
    )
    tool_call_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    tokens_in: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    tokens_out: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
        index=True,
    )

    thread: Mapped["ChatThread"] = relationship(back_populates="messages")
