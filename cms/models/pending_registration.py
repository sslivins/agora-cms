"""PendingRegistration ORM model.

Staging area for devices that have generated a keypair and pairing secret
and are waiting for an admin to adopt them via the QR code flow.

See umbrella issue #420 for the bootstrap redesign context.

A row is created by ``POST /api/devices/register`` and is keyed by
``pairing_secret_hash`` (SHA-256 of the device-generated pairing secret).
Adoption (``POST /api/devices/adopt``) looks up the row by hash, creates
a ``devices`` row, and writes the ECIES-encrypted bootstrap payload to
``outbox_ciphertext`` so the next ``GET /api/devices/bootstrap-status``
poll delivers it.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, Index, JSON, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from cms.database import Base


# Portable JSON column type: JSONB on PostgreSQL (for GIN-indexable querying
# if we ever want it), JSON on SQLite (for unit tests).
_JsonType = JSON().with_variant(JSONB(), "postgresql")


class PendingRegistration(Base):
    __tablename__ = "pending_registrations"
    __table_args__ = (
        Index("ix_pending_registrations_pubkey", "pubkey"),
        Index(
            "ix_pending_registrations_pubkey_unique",
            "pubkey",
            unique=True,
            postgresql_where=text("adopted_at IS NULL"),
            sqlite_where=text("adopted_at IS NULL"),
        ),
        Index("ix_pending_registrations_created_at", "created_at"),
        Index("ix_pending_registrations_polled_at", "polled_at"),
        {"extend_existing": True},
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    # Device-chosen identifier (Pi serial / hostname).  Not authoritative —
    # adoption is keyed by pairing_secret_hash, not device_id, to prevent
    # squatting.
    device_id: Mapped[str] = mapped_column(String(64), nullable=False)

    # Device ed25519 public key, base64-encoded.
    pubkey: Mapped[str] = mapped_column(Text, nullable=False)

    # SHA-256 hex digest of the raw pairing secret displayed in the QR.
    # This is what /adopt looks up against, so it is the effective primary
    # key from an application standpoint.  Unique to prevent collisions.
    pairing_secret_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )

    # Arbitrary metadata the device reported at registration
    # (firmware_version, device_type, hostname, etc.).
    connection_metadata: Mapped[dict | None] = mapped_column(_JsonType, nullable=True)

    # IP address seen on the /register request.  Used for rate-limit
    # bookkeeping and audit trails only.
    ip_address: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # Timestamps.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
    )

    # First successful /bootstrap-status poll — flips the row to the
    # longer TTL.  NULL means the row has never been polled, which
    # qualifies it for aggressive GC (1h) rather than the 24h TTL.
    polled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Set by /adopt.  NULL means still pending.
    adopted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # ECIES-encrypted bootstrap payload ({device_id, wps_jwt, wps_url,
    # jwt_expires_at}), base64-encoded.  Populated by /adopt, consumed by
    # /bootstrap-status.  NULL while the row is still pending.
    outbox_ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)

    # After adoption, the UUID of the created ``devices`` row.  Kept for
    # observability and to let the GC job verify the device exists before
    # deleting the pending row.
    adopted_device_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
