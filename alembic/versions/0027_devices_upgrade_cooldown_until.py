"""Add ``devices.upgrade_cooldown_until`` column for the upgrade endpoint's
send-failure cooldown (issue agora-cms#511).

Before this revision, the upgrade endpoint's three failure paths all
called ``_release_claim()`` to clear ``upgrade_started_at`` back to
NULL.  That's correct for the 503 (bundle not cached) and 409 (already
on target) fast-fail paths -- both return the same answer on an
immediate retry, so a held claim only adds friction -- but it's wrong
for the 502 (send-to-device failed) path: a user who double-clicks
"Update" while the first send is in flight gets back-to-back 502s
because both requests see ``upgrade_started_at IS NULL`` after each
other's failure rolls back the claim.

The simplest backdated-marker trick (write a stale ``upgrade_started_at``
so it auto-expires in ~10s via the existing TTL) doesn't work because
``_is_upgrading()`` reads the same column, so a backdated marker (which
is 14m50s "in the past") still reads as within ``UPGRADE_TTL = 15m`` --
the UI would show "Upgrading..." for 10s with no upgrade actually in
flight, which is more confusing than the original race.

Solution: a separate ``upgrade_cooldown_until`` timestamptz column.
The CAS-claim SQL adds a clause requiring this column to be either
NULL or in the past; ``_is_upgrading()`` does NOT inspect this column,
so the "Upgrading..." badge correctly stays off during the cooldown
window.  The send-failure rollback both clears ``upgrade_started_at``
to NULL and sets ``upgrade_cooldown_until = now() + 10s`` in a single
UPDATE.

The column is nullable with no server-side default so the existing
fleet rows pick up NULL on upgrade, exactly what we want (no
back-pressure on any device that wasn't mid-failure at migration time).

Revision ID: 0027
Revises: 0026
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "upgrade_cooldown_until",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade of 0027 is intentionally not implemented per project policy."
    )
