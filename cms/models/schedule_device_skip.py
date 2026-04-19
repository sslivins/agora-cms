"""Per-device schedule skip ORM model.

Used by the "End Now" feature so an operator can end a schedule for a single
device without affecting other devices that share the same schedule (e.g. via
a group target).  When ``device_id`` is used the scheduler skips just that
device; a schedule-wide skip is still persisted on ``Schedule.skipped_until``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base


class ScheduleDeviceSkip(Base):
    __tablename__ = "schedule_device_skips"

    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("schedules.id", ondelete="CASCADE"),
        primary_key=True,
    )
    device_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("devices.id", ondelete="CASCADE"),
        primary_key=True,
    )
    skip_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
