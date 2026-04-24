"""partial unique index on devices.pubkey

Bootstrap redesign (umbrella issue #420): enforces that no two devices
can be adopted with the same ed25519 public key.  Must land before any
code path writes a real ``devices.pubkey`` value (i.e. before Stage A.3
endpoints).

Uses a *partial* index ``WHERE pubkey IS NOT NULL`` so that:

- the many existing rows with ``pubkey IS NULL`` (legacy API-key devices)
  don't all collide on a shared NULL value, and
- pubkey uniqueness is only enforced once a device actually has one.

SQLite supports partial indexes as of 3.8.0 which covers our test matrix.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from alembic import op


revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_devices_pubkey_unique",
        "devices",
        ["pubkey"],
        unique=True,
        postgresql_where="pubkey IS NOT NULL",
        sqlite_where="pubkey IS NOT NULL",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0011 is not supported")
