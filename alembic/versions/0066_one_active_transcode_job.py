"""One active VARIANT_TRANSCODE job per variant.

Nothing ever enforced that a variant had at most one live job.  The stale
monitor manufactured replacements for transcodes that were merely slow (see
0065), and each replacement was claimed by its own worker, so several
workers transcoded the same variant to the same output blob concurrently.
0065 removed the source of those duplicates; this adds the constraint that
would have made them impossible in the first place, turning a silent
correctness bug into a loud insert-time error.

Scope note: the index is deliberately restricted to ``VARIANT_TRANSCODE``.
Other job types legitimately re-enqueue the same target — re-synthesising a
voice announcement after an edit, re-importing a failed base image,
re-capturing a stream — and a blanket constraint would turn those into 500s.

``CREATE INDEX`` is used rather than ``CREATE INDEX CONCURRENTLY``:
CONCURRENTLY cannot run inside a transaction, so a failure would leave an
INVALID index behind and the migration only partly applied.  Because the CMS
runs ``alembic upgrade head`` on startup with minScale=2, a partly-applied
migration is the revision-activation failure mode we have hit before.  The
predicate is narrow and ``jobs`` is small, so the build is brief; doing the
cleanup and the index in one transaction means either both land or neither
does.

Revision ID: 0066
Revises: 0065
"""

from __future__ import annotations

from alembic import op


revision = "0066"
down_revision = "0065"
branch_labels = None
depends_on = None


INDEX_NAME = "uq_jobs_one_active_per_variant"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        # SQLite (tests) builds its schema from the models via create_all,
        # which carries the same Index definition.
        return

    # Cleanup must precede the index or the build fails on existing rows.
    #
    # Keep the liveliest duplicate rather than simply the newest: after 0065
    # the row with the most recent heartbeat is the one a worker is actually
    # running, and the spurious replacements are the ones with no heartbeat
    # at all.  Terminalising the real worker's row instead would leave the
    # job it reports into pointing at a FAILED record.
    op.execute(
        """
        WITH ranked AS (
            SELECT id,
                   ROW_NUMBER() OVER (
                       PARTITION BY target_id
                       ORDER BY
                           (status = 'PROCESSING') DESC,
                           heartbeat_at DESC NULLS LAST,
                           created_at DESC
                   ) AS rn
            FROM jobs
            WHERE type = 'VARIANT_TRANSCODE'
              AND status IN ('PENDING', 'PROCESSING')
        )
        UPDATE jobs
           SET status = 'FAILED',
               error_message = 'superseded: duplicate active job for this variant',
               completed_at = now()
         WHERE id IN (SELECT id FROM ranked WHERE rn > 1)
        """
    )

    op.execute(
        f"""
        CREATE UNIQUE INDEX {INDEX_NAME}
            ON jobs (target_id)
         WHERE type = 'VARIANT_TRANSCODE'
           AND status IN ('PENDING', 'PROCESSING')
        """
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0066 is not supported")
