"""Fleet identity table — DB-backed replacement for the env-only registry.

Fleets used to live in ``Settings.fleet_register_secrets`` (a JSON env
var). That made "create a new fleet" a deploy-time operation — operators
had to add a GH Actions secret and roll the container.

This table moves the source of truth into Postgres so fleets can be
managed at runtime via the imager API. The HMAC secret material is
server-generated (``secrets.token_bytes(32)``, base64-encoded) and
**never** returned through the API. ``deleted_at`` is a soft-delete
column so old ``provisioned_images.fleet_id`` audit values still resolve
to a row showing "fleet-X (deleted)" in history views.

Companion service: ``cms/services/fleet_registry.py``.
Companion endpoints: ``cms/routers/imager.py`` — GET / POST / DELETE
``/api/imager/fleets``.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Index, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from shared.database import Base


class Fleet(Base):
    """One fleet identity (fleet_id + HMAC secret).

    Every Pi that registers via ``POST /api/devices/register`` has a
    ``fleet_id`` baked into ``/boot/firmware/agora-fleet.env`` plus an
    HMAC over the registration body keyed by this row's
    ``secret_b64``. Without a matching row in this table, the register
    is rejected with 401 (secure-by-default — empty table = no
    devices can register).

    ``secret_b64`` is base64-encoded raw bytes (typically 32 bytes of
    ``secrets.token_bytes``). Stored plaintext: an attacker with
    DB-read already has full access to every other tenant secret in
    Postgres (device API keys, asset SAS, etc.), so encrypting just
    fleet HMACs adds minimal defense-in-depth without a dedicated
    KMS pass. Tracked separately for the future if/when the whole
    secret surface gets a KMS rewrap.

    ``deleted_at`` is set by the soft-delete path. ``get_fleet_secret``
    filters ``deleted_at IS NULL``. The row is kept so audit views
    can still render the fleet name on old ``provisioned_images``.
    """

    __tablename__ = "fleets"
    __table_args__ = (
        Index(
            "uq_fleets_fleet_id_active",
            "fleet_id",
            unique=True,
            postgresql_where="deleted_at IS NULL",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4,
    )
    # Operator-chosen fleet identifier. Validated by ``_FLEET_ID_RE`` in
    # the imager router: ``^[A-Za-z0-9._-]{1,64}$``.
    fleet_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Base64-encoded HMAC secret. Never returned by any API endpoint.
    secret_b64: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
    )
    # Audit only — keep the row if the user is later deleted.
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True,
    )
