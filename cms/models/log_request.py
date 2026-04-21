"""LogRequest ORM model — Stage 3 outbox row for device log collection.

Backs the multi-replica-safe ``request_logs`` flow.  The UI creates a row
(``pending``); a drainer on every CMS replica (with ``SKIP LOCKED``)
picks pending rows and dispatches ``REQUEST_LOGS`` over the transport
(``sent``); the Pi uploads its gzipped bundle to CMS which streams it
into blob storage and flips the row (``ready``).  Failures bump
``attempts`` and store ``last_error``; the reaper deletes blobs past
``expires_at``.

See ``alembic/versions/0005_log_requests_table.py`` for the schema and
``docs/multi-replica-architecture.md`` §Stage 3 for the design.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import BigInteger, DateTime, ForeignKey, Index, Integer, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


# Status string values.  Kept as module-level constants (not a Python
# Enum) so the drainer / reaper / tests can reference them without
# coupling to SQLAlchemy's Enum type reflection.
STATUS_PENDING = "pending"
STATUS_SENT = "sent"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_EXPIRED = "expired"

TERMINAL_STATUSES = frozenset({STATUS_READY, STATUS_FAILED, STATUS_EXPIRED})


class LogRequest(Base):
    __tablename__ = "log_requests"
    __table_args__ = (
        # Drainer scan — pending rows oldest-first.
        Index("ix_log_requests_status_created", "status", "created_at"),
        # Per-device history lookup for the UI.
        Index("ix_log_requests_device_created", "device_id", "created_at"),
        # Reaper scan — migration creates a partial index on Postgres
        # (``WHERE expires_at IS NOT NULL``), but Alembic autogenerate
        # still treats the plain ``Index`` here as a match because the
        # partial predicate is declared via ``postgresql_where`` at the
        # migration layer.  See alembic/versions/0005_*.py.
        Index("ix_log_requests_expires_at", "expires_at"),
        {"extend_existing": True},
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    device_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("devices.id", ondelete="CASCADE"),
        nullable=False,
    )
    # Audit — who requested the logs.  Nullable so system-initiated
    # collection (future health probes) can land without a user.
    requested_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    # Optional list[str] of systemd service names; NULL = all services.
    services: Mapped[list[str] | None] = mapped_column(JSONB, nullable=True)
    since: Mapped[str] = mapped_column(
        Text, nullable=False, default="24h", server_default=text("'24h'"),
    )
    status: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=STATUS_PENDING,
        server_default=text("'pending'"),
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default=text("0"),
    )
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    ready_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    # Storage-backend-relative path to the uploaded bundle.  Populated
    # when the Pi's upload completes.
    blob_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    # Reaper deletes the blob + row after this timestamp.  Nullable so
    # rows with no expiry (e.g., for forensic retention) can opt out.
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        server_default=text("CURRENT_TIMESTAMP"),
    )

    device = relationship("Device")
