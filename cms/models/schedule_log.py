"""Schedule history log ORM model."""

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base


class ScheduleLogEvent(str, enum.Enum):
    """Types of schedule history events."""
    STARTED = "STARTED"       # Schedule began playing on a device
    ENDED = "ENDED"           # Schedule finished its time window normally
    SKIPPED = "SKIPPED"       # Admin used "End Now" to skip the schedule
    MISSED = "MISSED"         # Device was offline when schedule was due


class ScheduleLog(Base):
    __tablename__ = "schedule_logs"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Denormalized — schedule may be deleted later, we still want the history
    schedule_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("schedules.id", ondelete="SET NULL"), nullable=True
    )
    schedule_name: Mapped[str] = mapped_column(String(200), nullable=False)

    device_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("devices.id", ondelete="SET NULL"), nullable=True
    )
    device_name: Mapped[str] = mapped_column(String(200), nullable=False)

    asset_filename: Mapped[str] = mapped_column(String(255), nullable=False)

    event: Mapped[ScheduleLogEvent] = mapped_column(
        Enum(ScheduleLogEvent), nullable=False
    )

    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc), nullable=False
    )

    # Optional extra info (e.g. "Device offline since 2:30 PM")
    details: Mapped[str | None] = mapped_column(Text, nullable=True)
