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
    DISPLAY_CONNECTED = "display_connected"
    DISPLAY_DISCONNECTED = "display_disconnected"
    ERROR = "error"
    ERROR_CLEARED = "error_cleared"
    CMS_STARTED = "cms_started"
    CMS_STOPPED = "cms_stopped"
    # OTA lifecycle events — one per device-side FSM transition.
    # Persisted to ``device_events`` for the audit/event-log surface;
    # also drive the UI badge via ``cms.services.ota_progress``.  See
    # ``WIRE_TO_CMS_EVENT`` in ``cms.services.device_inbound`` for the
    # wire-format → enum mapping.
    OTA_DOWNLOAD_STARTED = "ota_download_started"
    OTA_DOWNLOAD_PROGRESS = "ota_download_progress"
    OTA_SIGNATURE_VERIFIED = "ota_signature_verified"
    OTA_STAGED = "ota_staged"
    OTA_STAGE_PROGRESS = "ota_stage_progress"
    OTA_EXTRACT_PROGRESS = "ota_extract_progress"
    OTA_TRYBOOT_INITIATED = "ota_tryboot_initiated"
    OTA_SLOT_CONFIRMED = "ota_slot_confirmed"
    OTA_PROMOTED = "ota_promoted"
    OTA_MIGRATION_COMPLETE = "ota_migration_complete"
    OTA_FAILED = "ota_failed"
    OTA_DECLINED = "ota_declined"
    # Synthetic CMS-side event emitted when the OTA projection columns
    # are auto-cleared after detecting a successful upgrade for which
    # we never received the terminal lifecycle event.  Two emit sites:
    # the register-path auto-recovery in ws.py / wps_webhook.py and
    # the startup backfill in main.py.  Not produced by any wire event.
    OTA_AUTO_CLEARED = "ota_auto_cleared"


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
        String(40), nullable=False, index=True,
    )
    details: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )

    device: Mapped["Device | None"] = relationship()
    group: Mapped["DeviceGroup | None"] = relationship()
