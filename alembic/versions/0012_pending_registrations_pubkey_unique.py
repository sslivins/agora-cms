"""partial unique index on pending_registrations.pubkey

Bootstrap redesign (umbrella issue #420), Stage A.3: ensures that at
most **one unadopted** pending_registrations row exists per ed25519
public key at any time.  Without this, two concurrent ``/register``
calls with the same pubkey (legitimate re-registrations of a rebooting
device, or an attacker racing to squat on a pubkey) could create
duplicate pending rows and ``GET /bootstrap-status?pubkey=<k>`` would
have to pick one — the wrong one, potentially.

The index is partial (``WHERE adopted_at IS NULL``) so that after a
row transitions to adopted it no longer participates in the uniqueness
check.  That lets the row linger with its encrypted outbox payload
until the device polls and fetches it (or the GC reaper deletes it)
without blocking a future re-registration by the same device.

SQLite 3.8+ supports partial indexes.

Revision ID: 0012
Revises: 0011
"""

from __future__ import annotations

from alembic import op


revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_pending_registrations_pubkey_unique",
        "pending_registrations",
        ["pubkey"],
        unique=True,
        postgresql_where="adopted_at IS NULL",
        sqlite_where="adopted_at IS NULL",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0012 is not supported")
