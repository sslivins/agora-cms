"""stage 4: leader_leases table

Revision ID: 0006
Revises: 0005
Create Date: 2026-04-22

Introduces the table-backed leader-lease primitive used to elect a
single replica to run loops where bounded-time failover matters
(``scheduler_loop`` and ``service_key_rotation_loop`` in the first
rollout). Each lease row is identified by ``loop_name`` and held by
``holder_id`` (a per-process UUID) until ``expires_at``. A background
heartbeat inside :class:`cms.services.leader.LeaderLease` renews the
row with a conditional ``UPDATE ... WHERE expires_at < NOW() OR
holder_id = :me``; if the current holder dies, a standby picks up the
lease within one TTL.

Postgres-only: on SQLite (unit tests) :mod:`cms.services.leader` short
circuits and always reports leadership, so this migration is a no-op
there.

See ``docs/multi-replica-architecture.md`` §Stage 4 and issue #344.
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "leader_leases",
        # One row per elected loop (e.g. "scheduler", "service_key_rotation").
        sa.Column("loop_name", sa.Text(), primary_key=True),
        # Per-process UUID of the replica currently holding the lease.
        # Not an FK to any other table — identity is ephemeral.
        sa.Column("holder_id", sa.Text(), nullable=False),
        # Lease is valid iff NOW() < expires_at. Takeover condition:
        # `expires_at < NOW() OR holder_id = :me` (renewal is idempotent).
        sa.Column(
            "expires_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        # Observability: last heartbeat time. Not used for correctness.
        sa.Column(
            "renewed_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
    )


def downgrade() -> None:
    op.drop_table("leader_leases")
