"""VoiceAnnouncement model — backing data for a TTS announcement asset.

One row in this table is bound 1:1 to an :class:`Asset` row of
:attr:`AssetType.VOICE_ANNOUNCEMENT`. The asset row carries cache/runtime
metadata for the synthesized audio bytes; this row carries the authoring
script plus generation lifecycle state.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, String, Text, text
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.database import Base
from shared.models.job import JobStatus


class VoiceAnnouncement(Base):
    """Authoring + generation-state row for a voice-announcement asset."""

    __tablename__ = "voice_announcements"

    id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("assets.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    script_text: Mapped[str] = mapped_column(Text, nullable=False)
    voice_name: Mapped[str] = mapped_column(String(128), nullable=False)
    emotion: Mapped[str | None] = mapped_column(String(64), nullable=True)
    language: Mapped[str] = mapped_column(
        String(16), nullable=False, default="en-US", server_default="en-US"
    )
    speech_rate: Mapped[str | None] = mapped_column(String(16), nullable=True)
    generation_status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus),
        nullable=False,
        default=JobStatus.PENDING,
        server_default=JobStatus.PENDING.name,
    )
    generation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )

    asset = relationship(
        "Asset",
        foreign_keys=[asset_id],
        back_populates="voice_announcement",
    )
