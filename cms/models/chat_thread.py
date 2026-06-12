"""Chat assistant: persisted conversation thread.

Phase 1 of the in-CMS Assistant feature.  Each thread is a strictly
per-user conversation; there is no sharing, no org-level visibility,
and no admin escape hatch.  When a user is removed (or simply
de-allowlisted) their threads stay in the table — soft data, not
auth state — until they explicitly delete them.

Soft semantics for ``title``:  the agent loop will summarise the
first user message and stamp ``title`` after the first turn; until
then the column is empty and the UI shows the first message as a
placeholder.  Empty is the only valid "no title yet" sentinel.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


class ChatThread(Base):
    __tablename__ = "chat_threads"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    mode: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="general",
        server_default="general",
    )
    # Binds an editor-mode thread to the one asset it is editing.  For
    # ``composed_editor`` threads this is the composed-slide asset; for
    # ``slideshow_editor`` threads the same column is reused to point at
    # the slideshow asset.  NULL for general-mode threads.  The agent
    # forces this asset id onto the editor's asset-scoped tools so the
    # editor assistant can only ever read/write *this* asset, never
    # another one.  ``ondelete=CASCADE``: editor chats are asset-scoped
    # ephemera, so deleting the asset deletes its chat.
    composed_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
        index=True,
    )

    messages: Mapped[list["ChatMessage"]] = relationship(
        "ChatMessage",
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="ChatMessage.created_at",
    )
