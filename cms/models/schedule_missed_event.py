"""Scheduler MISSED-event dedup ORM model.

Persists the dedup + grace-clock state for scheduler MISSED alerts
that was previously held in the module-level ``_missed_logged`` and
``_offline_since`` dicts in ``cms.services.scheduler``.

Under N>1 replicas the scheduler loop is leader-gated, so only one
replica at a time writes these rows under normal operation.  The
table's value is realised on **leader failover** (deploy rollover,
pod crash): without it, the new leader starts with empty memory and
would (a) restart the grace clock from zero, delaying MISSED
emission, and (b) re-emit MISSED for a schedule+device combo the
prior leader already alerted on.

Schema choices:
  * PK ``(schedule_id, device_id, occurrence_date)`` — dedup key is
    per calendar day in the server's local timezone.  Overnight
    schedules therefore get "max one MISSED per local day", which is
    acceptable semantics for this alert type.
  * ``first_seen_offline_at`` preserves the current grace-clock
    semantics (clock starts when the scheduler first observes
    ``active schedule × offline device``, *not* when the device
    disconnected).
  * ``emitted_at`` is NULL until the CAS UPDATE claims the emission;
    non-NULL thereafter.  Serves as the dedup flag across replicas.
  * No foreign keys — we clean up stale rows via the scheduler's own
    pruning pass, and avoiding FKs removes the transaction-rollback
    hazard that ``_log_event`` has on FK violations.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime

from sqlalchemy import Date, DateTime, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base


class ScheduleMissedEvent(Base):
    __tablename__ = "schedule_missed_events"

    schedule_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True,
    )
    device_id: Mapped[str] = mapped_column(
        String(64), primary_key=True,
    )
    occurrence_date: Mapped[date] = mapped_column(
        Date(), primary_key=True,
    )
    first_seen_offline_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False,
    )
    emitted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
