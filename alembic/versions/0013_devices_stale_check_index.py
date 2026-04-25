"""partial index on devices(last_seen) WHERE online = true

PR #440 — supports the leader-gated stale-presence sweep
(:func:`alert_service.stale_presence_sweep_once`). The sweep claims
devices whose ``last_seen`` is older than a threshold while still
marked ``online = TRUE``; this partial index keeps the claim fast on
fleets where most devices are healthy (the index only contains rows
that are *currently* online, which is the only set the sweep ever
scans).

Both Postgres and SQLite (3.8+) support partial indexes via the
``WHERE`` clause; alembic surfaces it via ``postgresql_where`` /
``sqlite_where``.

Revision ID: 0013
Revises: 0012
"""

from __future__ import annotations

from alembic import op


revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_devices_stale_check",
        "devices",
        ["last_seen"],
        postgresql_where="online = true AND last_seen IS NOT NULL",
        sqlite_where="online = 1 AND last_seen IS NOT NULL",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0013 is not supported")
