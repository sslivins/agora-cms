"""Restore ``devices.group_id`` as the single group authority (#863 follow-up).

Reverses the Stage 8b contract (0057). Many-to-many device↔group *authority*
turned out to be unenforceable: group access is granted via ``UserGroup`` rows
behind ``users:write``, which Operators do not hold, so an Operator who put a
shared device into a second group could neither be told about the resulting
schedule conflict (the other group's schedules are outside their read scope)
nor fix it. See the design discussion on #863.

The multi-purpose-device use case that motivated many-to-many is served
instead by *selectors* that narrow a schedule to a subset of its owning
group's devices — labels that carry no ACL of their own.

Backfill picks the lowest ``group_id`` per device so the result is
deterministic; the join table carries no ordering column and the feature was
never deployed to production, so no device has a meaningful "primary" group to
preserve.

Revision ID: 0058
Revises: 0057
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision = "0058"
down_revision = "0057"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("group_id", UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_devices_group_id_device_groups",
        "devices",
        "device_groups",
        ["group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_devices_group_id", "devices", ["group_id"])

    # Collapse each device's membership set to a single owning group.
    op.execute(
        """
        UPDATE devices
        SET group_id = (
            SELECT m.group_id
            FROM device_group_memberships m
            WHERE m.device_id = devices.id
            ORDER BY CAST(m.group_id AS TEXT)
            LIMIT 1
        )
        """
    )

    op.drop_table("device_group_memberships")


def downgrade() -> None:
    raise NotImplementedError(
        "Forward-only, matching 0057. Recovery is via forward repair. See #863."
    )
