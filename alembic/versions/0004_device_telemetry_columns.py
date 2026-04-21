"""stage 2c: device presence + telemetry columns

Revision ID: 0004
Revises: 0003
Create Date: 2026-04-22

Adds telemetry + presence columns to ``devices`` so status heartbeats
persist across replicas and the UI can render live state from the DB
rather than the per-replica in-memory registry.

See issue #344 (multi-replica refactor, Stage 2c) and plan.md.

``fillfactor=80`` reserves space in each page for HOT-update of the
high-churn telemetry columns (STATUS lands every 30s per device).
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Presence
    op.add_column(
        "devices",
        sa.Column(
            "online", sa.Boolean(), nullable=False, server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "devices",
        sa.Column("connection_id", sa.Text(), nullable=True),
    )
    # Monotonic guard for STATUS writes
    op.add_column(
        "devices",
        sa.Column("last_status_ts", sa.DateTime(timezone=True), nullable=True),
    )
    # Health
    op.add_column(
        "devices",
        sa.Column("cpu_temp_c", sa.Float(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("load_avg", sa.Float(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column(
            "uptime_seconds",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
        ),
    )
    # Playback
    op.add_column(
        "devices",
        sa.Column(
            "mode",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'unknown'"),
        ),
    )
    op.add_column(
        "devices",
        sa.Column("asset", sa.Text(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column(
            "pipeline_state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'NULL'"),
        ),
    )
    op.add_column(
        "devices",
        sa.Column(
            "playback_started_at", sa.DateTime(timezone=True), nullable=True,
        ),
    )
    op.add_column(
        "devices",
        sa.Column("playback_position_ms", sa.Integer(), nullable=True),
    )
    # Error
    op.add_column(
        "devices",
        sa.Column("error", sa.Text(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("error_since", sa.DateTime(timezone=True), nullable=True),
    )
    # Device-side toggles / display
    op.add_column(
        "devices",
        sa.Column("ssh_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("local_api_enabled", sa.Boolean(), nullable=True),
    )
    op.add_column(
        "devices",
        sa.Column("display_connected", sa.Boolean(), nullable=True),
    )

    # HOT-friendly page layout for the high-churn telemetry columns.
    # Applies to new pages; existing small rows get rewritten organically.
    # Skipped on non-Postgres dialects (SQLite dev/test).
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE devices SET (fillfactor = 80)")


def downgrade() -> None:
    raise NotImplementedError(
        "Project policy: migrations are forward-only. "
        "Roll forward with a new migration if a schema correction is needed."
    )
