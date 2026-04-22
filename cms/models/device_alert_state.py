"""DeviceAlertState — per-device alert state for multi-replica safety.

Backs the ``cms.services.alert_service`` loop under N>1 replicas so the
"device has been offline for ≥ grace period, fire notification" logic
stops depending on replica-local asyncio timer tasks.  See
``alembic/versions/0007_alert_state_upgrade_claim.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base


class DeviceAlertState(Base):
    """Persisted per-device alert flags.

    ``offline_since`` is set by the disconnect path *only* when it is
    currently NULL (online→offline transition) — duplicate disconnects
    don't reset the grace window.  The leader-gated offline-sweep loop
    flips ``offline_notified`` to TRUE once the grace period has
    elapsed and emits the notification.  The reconnect path clears
    both fields in a single CAS and, if ``offline_notified`` was
    previously TRUE, emits the "back online" notification in the same
    transaction.
    """

    __tablename__ = "device_alert_state"
    __table_args__ = (
        Index(
            "ix_device_alert_state_pending",
            "offline_since",
            postgresql_where=text("offline_notified = false"),
        ),
        {"extend_existing": True},
    )

    device_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("devices.id", ondelete="CASCADE"),
        primary_key=True,
    )
    offline_since: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    offline_notified: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default=text("false"),
    )
