"""pending_registrations table + devices.pubkey column

Bootstrap redesign (umbrella issue #420): adds storage for the
HTTPS-based device onboarding flow (ed25519 identity + QR pairing).

- ``pending_registrations``: staging table for devices between
  ``POST /api/devices/register`` and ``POST /api/devices/adopt``.
  Carries the device's public key, the pairing secret hash (adoption
  lookup key), and — once adopted — the ECIES-encrypted bootstrap
  payload waiting to be polled by the device.
- ``devices.pubkey``: ed25519 public key (base64).  NULL during the
  coexistence window for legacy devices that still authenticate with
  an API key; populated at adoption time for new devices and by the
  Stage C migration path for existing devices.

This migration intentionally does NOT drop ``device_api_key_hash`` /
``previous_api_key_hash`` / ``api_key_rotated_at`` / ``device_auth_token_hash``.
Those columns remain in use by the legacy direct-WS bootstrap path
through Stages B and C and are dropped in Stage D once the fleet has
fully migrated.

Revision ID: 0010
Revises: 0009
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB, UUID


# revision identifiers, used by Alembic.
revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "sqlite"

    # ---- pending_registrations ----
    if dialect == "postgresql":
        id_col = sa.Column(
            "id", UUID(as_uuid=True), primary_key=True, nullable=False,
        )
        metadata_col = sa.Column("connection_metadata", JSONB, nullable=True)
    else:
        # SQLite (unit tests): UUIDs as CHAR(36), JSON as TEXT.
        id_col = sa.Column(
            "id", sa.String(36), primary_key=True, nullable=False,
        )
        metadata_col = sa.Column("connection_metadata", sa.JSON(), nullable=True)

    op.create_table(
        "pending_registrations",
        id_col,
        sa.Column("device_id", sa.String(length=64), nullable=False),
        sa.Column("pubkey", sa.Text(), nullable=False),
        sa.Column(
            "pairing_secret_hash",
            sa.String(length=64),
            nullable=False,
            unique=True,
        ),
        metadata_col,
        sa.Column("ip_address", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("polled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("adopted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outbox_ciphertext", sa.Text(), nullable=True),
        sa.Column("adopted_device_id", sa.String(length=64), nullable=True),
    )
    op.create_index(
        "ix_pending_registrations_pubkey",
        "pending_registrations",
        ["pubkey"],
    )
    op.create_index(
        "ix_pending_registrations_created_at",
        "pending_registrations",
        ["created_at"],
    )
    op.create_index(
        "ix_pending_registrations_polled_at",
        "pending_registrations",
        ["polled_at"],
    )

    # ---- devices.pubkey ----
    op.add_column(
        "devices",
        sa.Column("pubkey", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrade is not supported for this migration.")
