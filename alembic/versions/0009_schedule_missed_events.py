"""schedule_missed_events table (N>1 failover dedup for MISSED alerts)

Issue #344 multi-replica audit — MEDIUM severity.

Persists the scheduler's MISSED-event dedup + grace-clock state that
was previously held in ``cms.services.scheduler._missed_logged`` and
``_offline_since`` module-level dicts.  The scheduler loop is leader-
gated, so under normal operation only one replica writes these rows,
but on leader failover (deploy rollover / pod crash) the new leader
would otherwise start with empty memory — (a) restarting the grace
clock from zero, delaying MISSED emission, and (b) re-emitting MISSED
for schedule+device combos the prior leader already alerted on.

No foreign keys: stale rows are cleaned up by the scheduler's own
pruning pass, and we deliberately avoid FKs so a concurrent delete of
a schedule or device can't trigger the session-wide rollback hazard
that ``_log_event`` has on FK violations.

Revision ID: 0009
Revises: 0008
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


# revision identifiers, used by Alembic.
revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name if bind is not None else "sqlite"
    if dialect == "postgresql":
        schedule_id_col = sa.Column(
            "schedule_id", UUID(as_uuid=True), primary_key=True, nullable=False,
        )
    else:
        # SQLite (unit tests): use CHAR(36) for UUIDs.
        schedule_id_col = sa.Column(
            "schedule_id", sa.String(36), primary_key=True, nullable=False,
        )

    op.create_table(
        "schedule_missed_events",
        schedule_id_col,
        sa.Column("device_id", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column("occurrence_date", sa.Date(), primary_key=True, nullable=False),
        sa.Column(
            "first_seen_offline_at", sa.DateTime(timezone=True), nullable=False,
        ),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrade is not supported for this migration.")
