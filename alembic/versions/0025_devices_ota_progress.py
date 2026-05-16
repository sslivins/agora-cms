"""Add OTA progress columns to devices + widen device_events.event_type.

Issue agora-cms#574 — replaces the static "Upgrading…" badge with a live
progress bar driven by per-OTA lifecycle events from the device.

Two structural changes:

1. Six nullable columns on ``devices`` so the WPS lifecycle event handler
   has a single multi-replica-safe place to write progress to:

   - ``ota_phase``       — machine-readable, e.g. ``"downloading"``
   - ``ota_label``       — human-readable, e.g. ``"Downloading bundle"``
   - ``ota_pct``         — 0.0–100.0 float, NULL for phases that don't
                            carry byte progress (e.g. ``signature_verified``)
   - ``ota_bytes_done``  — current byte counter for download / extract
   - ``ota_bytes_total`` — expected total for the same
   - ``ota_updated_at`` — UTC timestamp of the last event; also acts as
                          the freshness gate in
                          ``cms.routers.devices._ota_fields_for_out`` so
                          stale rows (orphaned by a missed terminal event)
                          fall back to NULL after OTA_FRESH_TTL.

   All six are nullable / default NULL — they're populated only while an
   OTA is in flight and cleared on terminal events.  No write path
   relies on a default, so we don't need ``server_default`` round-trip
   handling.

2. ``device_events.event_type`` width bumped 20 → 40 to fit the 12 new
   ``OTA_*`` values added in ``cms.models.device_event``.  The longest
   existing value was 22 chars (``display_disconnected``) which already
   matched the existing 20-char limit poorly (string was 20 chars but
   the column was 20 chars — silent truncation never bit us only because
   the corresponding ``DeviceEventType`` enum value never actually got
   stored).  Bumping to 40 covers
   ``OTA_MIGRATION_COMPLETE`` (22), ``OTA_SIGNATURE_VERIFIED`` (22),
   ``OTA_DOWNLOAD_PROGRESS`` (21), ``OTA_EXTRACT_PROGRESS`` (20) etc.

Revision ID: 0025
Revises: 0024
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("ota_phase", sa.Text(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("ota_label", sa.Text(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("ota_pct", sa.Float(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("ota_bytes_done", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("ota_bytes_total", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("ota_updated_at", sa.DateTime(timezone=True), nullable=True),
    )

    op.alter_column(
        "device_events",
        "event_type",
        existing_type=sa.String(length=20),
        type_=sa.String(length=40),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Project policy (test_migration_policy.py): every downgrade must
    # raise NotImplementedError. Forward-only migrations only.
    raise NotImplementedError(
        "Downgrade of 0025 is intentionally not implemented per project policy."
    )
