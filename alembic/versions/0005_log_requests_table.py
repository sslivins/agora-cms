"""stage 3a: log_requests outbox table

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-25

Introduces the durable outbox that backs the multi-replica log RPC.
A row is created when the UI asks for device logs; a drainer on every
replica picks ``pending`` rows (``SKIP LOCKED``) and dispatches
``REQUEST_LOGS`` over the transport; the Pi uploads its bundle to CMS
which streams it into blob storage and flips the row to ``ready``.

Status lifecycle (enforced by ``cms.services.log_outbox``):

    pending ─► sent ─► ready
                 └─► failed
                 └─► expired

See issue #345 (multi-replica refactor, Stage 3) and
``docs/multi-replica-architecture.md`` §Stage 3 for the locked design.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    is_postgres = bind.dialect.name == "postgresql"

    # ``services`` holds an optional list[str] of systemd service names
    # to filter.  JSONB on Postgres, JSON on SQLite (tests).
    services_type = postgresql.JSONB(astext_type=sa.Text()) if is_postgres else sa.JSON()

    op.create_table(
        "log_requests",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(length=64),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        # Audit — who asked.  Nullable so system-initiated requests (future
        # health probes, scheduled collection) can land without a user.
        sa.Column(
            "requested_by_user_id",
            postgresql.UUID(as_uuid=True) if is_postgres else sa.String(length=36),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("services", services_type, nullable=True),
        sa.Column(
            "since",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'24h'"),
        ),
        # Lifecycle: pending | sent | ready | failed | expired.
        # Checked at the app layer (enum values kept out of Postgres so new
        # states can be added without migrations).
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'pending'"),
        ),
        sa.Column(
            "attempts",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
        sa.Column("last_error", sa.Text(), nullable=True),
        # Timestamps for each lifecycle transition.
        sa.Column(
            "sent_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "ready_at", sa.DateTime(timezone=True), nullable=True,
        ),
        # Blob location (storage-backend-relative path), size, expiry for
        # the reaper to delete old artefacts.
        sa.Column("blob_path", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=True),
        sa.Column(
            "expires_at", sa.DateTime(timezone=True), nullable=True,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )

    # Drainer scans ``WHERE status = 'pending' ORDER BY created_at``.
    op.create_index(
        "ix_log_requests_status_created",
        "log_requests",
        ["status", "created_at"],
    )
    # Per-device history lookup for the UI.
    op.create_index(
        "ix_log_requests_device_created",
        "log_requests",
        ["device_id", "created_at"],
    )
    # Reaper scans expired rows.  Partial index on Postgres so we only
    # pay for rows that have an expiry set.
    if is_postgres:
        op.create_index(
            "ix_log_requests_expires_at",
            "log_requests",
            ["expires_at"],
            postgresql_where=sa.text("expires_at IS NOT NULL"),
        )
    else:
        op.create_index(
            "ix_log_requests_expires_at",
            "log_requests",
            ["expires_at"],
        )

    # HOT-friendly page layout: status/attempts/last_error/updated_at
    # all churn on every drainer tick.  Same trick as ``devices``.
    if is_postgres:
        op.execute("ALTER TABLE log_requests SET (fillfactor = 80)")


def downgrade() -> None:
    raise NotImplementedError(
        "Project policy: migrations are forward-only. "
        "Roll forward with a new migration if a schema correction is needed."
    )
