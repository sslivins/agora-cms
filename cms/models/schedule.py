"""Schedule ORM model."""

import uuid
from datetime import datetime, time, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Time
from sqlalchemy.dialects.postgresql import ARRAY, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


class Schedule(Base):
    __tablename__ = "schedules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200), nullable=False)

    # Target: device or group (one must be set)
    device_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("devices.id"), nullable=True
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_groups.id"), nullable=True
    )

    # What to play
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id"), nullable=False
    )

    # When — date range (nullable = open-ended)
    start_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    end_date: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Daily time window
    start_time: Mapped[time] = mapped_column(Time, nullable=False)
    end_time: Mapped[time] = mapped_column(Time, nullable=False)

    # Recurrence: list of ISO weekday numbers (1=Mon, 7=Sun). Empty = every day.
    days_of_week: Mapped[list[int] | None] = mapped_column(ARRAY(Integer), nullable=True)

    priority: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)

    # Exact loop count — None means infinite looping (fill the time window)
    loop_count: Mapped[int | None] = mapped_column(Integer, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    asset: Mapped["Asset"] = relationship(back_populates="schedules")
    device: Mapped["Device | None"] = relationship()
    group: Mapped["DeviceGroup | None"] = relationship()
