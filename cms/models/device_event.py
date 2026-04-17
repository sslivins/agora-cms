"""Device event log model — system health events distinct from audit/schedule logs."""

import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import DateTime, ForeignKey, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


class DeviceEventType(str, PyEnum):
    ONLINE = "online"
    OFFLINE = "offline"
    TEMP_HIGH = "temp_high"
    TEMP_CLEARED = "temp_cleared"
    CMS_STARTED = "cms_started"
    CMS_STOPPED = "cms_stopped"


class DeviceEvent(Base):
    __tablename__ = "device_events"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    device_name: Mapped[str] = mapped_column(
        String(200), nullable=False, default="",
        doc="Denormalized snapshot — readable even if device is later deleted",
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_groups.id", ondelete="SET NULL"),
        nullable=True,
    )
    group_name: Mapped[str] = mapped_column(
        String(100), nullable=False, default="",
        doc="Denormalized snapshot of group name at event time",
    )
    event_type: Mapped[str] = mapped_column(
        String(20), nullable=False, index=True,
    )
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    device: Mapped["Device | None"] = relationship()
    group: Mapped["DeviceGroup | None"] = relationship()
