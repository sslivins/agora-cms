"""Notification ORM model."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default="system",
        doc="Visibility scope: 'system', 'group', or 'user'",
    )
    level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="info",
        doc="Severity: 'info', 'success', 'warning', or 'error'",
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)

    # Scope targets (nullable — set based on scope value)
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_groups.id", ondelete="CASCADE"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    group: Mapped["DeviceGroup | None"] = relationship()
    user: Mapped["User | None"] = relationship()
