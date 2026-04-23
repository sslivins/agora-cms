"""DeviceAlertState — per-device alert state for multi-replica safety.

Backs the ``cms.services.alert_service`` loop under N>1 replicas so the
"device has been offline for ≥ grace period, fire notification" and
"device CPU temp crossed threshold, fire notification" logic stop
depending on replica-local dicts.  See
``alembic/versions/0007_alert_state_upgrade_claim.py`` and
``alembic/versions/0008_device_alert_state_temp.py``.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, text
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base


class DeviceAlertState(Base):
    """Persisted per-device alert flags.

    Offline detection
    -----------------
    ``offline_since`` is set by the disconnect path *only* when it is
    currently NULL (online→offline transition) — duplicate disconnects
    don't reset the grace window.  The leader-gated offline-sweep loop
    flips ``offline_notified`` to TRUE once the grace period has
    elapsed and emits the notification.  The reconnect path clears
    both fields in a single CAS and, if ``offline_notified`` was
    previously TRUE, emits the "back online" notification in the same
    transaction.

    Temperature monitoring
    ----------------------
    Temp alerts are event-driven (fired from STATUS heartbeats), not
    periodic, so they don't need leader election — the row-level
    ``SELECT ... FOR UPDATE`` inside ``check_temperature`` serializes
    concurrent replicas handling the same device.

    - ``temp_level`` tracks the last-observed level ("normal" /
      "warning" / "critical").  Default "normal" matches the
      pre-persistence behavior.
    - ``temp_last_alert_at`` is the timestamp of the last TEMP_HIGH /
      TEMP_CLEARED emission for this device; used for cooldown and
      reminder-path scheduling.
    - ``temp_last_sample_ts`` is the timestamp of the last STATUS
      sample that mutated this row; older samples arriving out of
      order are ignored.  Sourced from the WPS ``ce-time`` header
      (Azure server time) when available, else ``datetime.now(UTC)``.
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
    temp_level: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="normal",
        server_default=text("'normal'"),
    )
    temp_last_alert_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    temp_last_sample_ts: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
