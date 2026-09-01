"""Generic alert lifecycle rows for device incidents."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


class DeviceAlert(Base):
    """Durable open/resolved lifecycle state for one device alert kind."""

    __tablename__ = "device_alerts"
    __table_args__ = (
        UniqueConstraint("device_id", "kind", name="uq_device_alerts_device_kind"),
        Index("ix_device_alerts_state_kind", "state", "kind"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(String(20), nullable=False, default="open")
    incident_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), nullable=True, index=True
    )
    opened_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    raise_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_events.id", ondelete="SET NULL"),
        nullable=True,
    )
    resolve_event_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_events.id", ondelete="SET NULL"),
        nullable=True,
    )

    device: Mapped["Device"] = relationship()
    raise_event: Mapped["DeviceEvent | None"] = relationship(
        foreign_keys=[raise_event_id]
    )
    resolve_event: Mapped["DeviceEvent | None"] = relationship(
        foreign_keys=[resolve_event_id]
    )
