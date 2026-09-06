"""Group-scoped device tags + optional schedule tag target (#863 follow-up).

A device tag is a label attached to devices *within one group*. Tag identity is
``(group_id, lower(name))``, so "Summer Promos" in Group A and "Summer Promos"
in Group B are two unrelated tags. There is deliberately no global namespace:
"target every device tagged X" is unrepresentable rather than merely forbidden,
which keeps the owning group the sole authorization boundary.

``schedules.tag_id`` narrows a schedule from "every device in the group" to
"every device in the group carrying this tag". It is CASCADE-deleted with the
tag, since a schedule whose selector vanished has no meaningful target.

Revision ID: 0059
Revises: 0058
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID


revision = "0059"
down_revision = "0058"
branch_labels = None
depends_on = None

DEFAULT_DEVICE_TAG_COLOR = "#737373"


def upgrade() -> None:
    op.create_table(
        "device_tags",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "group_id",
            UUID(as_uuid=True),
            sa.ForeignKey("device_groups.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("name", sa.String(64), nullable=False),
        sa.Column(
            "color",
            sa.String(16),
            nullable=False,
            server_default=DEFAULT_DEVICE_TAG_COLOR,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.Column(
            "created_by_user_id",
            UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )
    op.create_index("ix_device_tags_group_id", "device_tags", ["group_id"])
    op.create_index(
        "uq_device_tags_group_name_lower",
        "device_tags",
        ["group_id", sa.text("lower(name)")],
        unique=True,
    )

    op.create_table(
        "device_tag_assignments",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "device_id",
            sa.String(64),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "tag_id",
            UUID(as_uuid=True),
            sa.ForeignKey("device_tags.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.UniqueConstraint("device_id", "tag_id", name="uq_device_tag_assignment"),
    )
    op.create_index(
        "idx_device_tag_assignments_tag_id", "device_tag_assignments", ["tag_id"]
    )

    op.add_column(
        "schedules", sa.Column("tag_id", UUID(as_uuid=True), nullable=True)
    )
    op.create_foreign_key(
        "fk_schedules_tag_id",
        "schedules",
        "device_tags",
        ["tag_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_index("ix_schedules_tag_id", "schedules", ["tag_id"])


def downgrade() -> None:
    op.drop_index("ix_schedules_tag_id", table_name="schedules")
    op.drop_constraint("fk_schedules_tag_id", "schedules", type_="foreignkey")
    op.drop_column("schedules", "tag_id")
    op.drop_index(
        "idx_device_tag_assignments_tag_id", table_name="device_tag_assignments"
    )
    op.drop_table("device_tag_assignments")
    op.drop_index("uq_device_tags_group_name_lower", table_name="device_tags")
    op.drop_index("ix_device_tags_group_id", table_name="device_tags")
    op.drop_table("device_tags")
