"""Device ↔ group many-to-many: ``device_group_memberships`` join table + backfill.

Stage 2 of the device-groups many-to-many rework (#863) — the *expand* half of
an expand/contract migration away from the single ``devices.group_id`` FK.

Migration shape
---------------
1. Create ``device_group_memberships`` (composite PK ``(device_id, group_id)``,
   both FKs ``ON DELETE CASCADE``) mirroring ``user_groups``.
2. Add the reverse-direction index ``(group_id, device_id)`` for
   "which devices are in this group" lookups.
3. **Non-failing backfill:** insert one membership per device that currently
   has a non-null ``group_id``. This is a 1:1 mirror, so it introduces no new
   cross-group schedule unions and therefore no new conflicts — existing
   runtime behaviour is preserved exactly. No hard uniqueness/at-most-one
   constraint is added at the DB level: at-most-one is enforced in application
   code during the coexistence window (dual-write), and true multi-membership
   is enabled in a later stage. Pre-existing within-group overlaps in the data
   are intentionally NOT failed here.

``devices.group_id`` is deliberately left in place; the scheduler keeps reading
it until a later stage flips reads onto the join table (contract phase).

Revision ID: 0055
Revises: 0054
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision = "0055"
down_revision = "0054"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "device_group_memberships",
        sa.Column(
            "device_id",
            sa.String(length=64),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("device_groups.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    op.create_index(
        "ix_device_group_memberships_group_device",
        "device_group_memberships",
        ["group_id", "device_id"],
    )

    # Non-failing 1:1 backfill from the legacy single-group FK.
    op.execute(
        "INSERT INTO device_group_memberships (device_id, group_id) "
        "SELECT id, group_id FROM devices WHERE group_id IS NOT NULL"
    )


def downgrade() -> None:
    # Project policy (test_migration_policy.py): every downgrade must
    # raise NotImplementedError. Forward-only migrations only.
    raise NotImplementedError(
        "Downgrade of 0055 is intentionally not implemented per project policy."
    )
