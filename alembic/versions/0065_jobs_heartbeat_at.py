"""Add jobs.heartbeat_at — worker liveness signal.

The stream-capture monitor used to decide a PROCESSING variant was stale by
measuring ``AssetVariant.created_at`` against a fixed timeout.  That measures
total elapsed wall-clock time, not liveness, so any transcode legitimately
slower than the timeout was declared dead and re-enqueued on every tick.  In
production a single 72-minute 1080p transcode produced three duplicate
workers, all writing the same output blob concurrently.

``AssetVariant.progress`` is not a usable substitute: it is only written when
ffmpeg reports a duration (never for livestreams or un-probeable inputs), it
stops entirely once the estimate clamps at 99%, and the image / thumbnail /
webpage branches jump 0 → 100 with nothing in between.

The worker already makes a DB round-trip every 15s to probe
``cancel_requested``; that statement now also stamps this column, so a real
liveness signal costs no extra load.

NULL means "claimed before this column existed".  The monitor coalesces to
``created_at`` in that case, so a deploy that lands mid-transcode does not
reap every in-flight job on the first tick.

Revision ID: 0065
Revises: 0064
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0065"
down_revision = "0064"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # Nullable with no server_default: a plain catalogue update on
    # PostgreSQL 11+, so no table rewrite and no long lock on the hot
    # ``jobs`` table.
    op.add_column(
        "jobs",
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
    )

    # Backfill in-flight jobs to "now" so the first monitor tick after the
    # deploy sees them as freshly alive rather than as (created_at < cutoff)
    # corpses.  Without this, any job claimed more than the stale threshold
    # ago would be reset and duplicated at exactly the moment we are trying
    # to stop duplicating.
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            "UPDATE jobs SET heartbeat_at = now() WHERE status = 'PROCESSING'"
        )


def downgrade() -> None:
    op.drop_column("jobs", "heartbeat_at")
