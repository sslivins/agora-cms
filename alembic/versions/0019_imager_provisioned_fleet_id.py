"""imager: add fleet_id audit column to provisioned_images

The PR 4 build API records the ``fleet_id`` the operator selected on
each ``provisioned_images`` row so the audit trail survives after the
worker clears ``fleet_env_payload`` on terminal success.

Nullable for backward-compatibility with existing rows (the table is
empty in practice but PR 3 worker tests insert rows without fleet_id).
PR 4's API populates it on insert.

Revision ID: 0019
Revises: 0018
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provisioned_images",
        sa.Column("fleet_id", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "downgrade not implemented; restore from backup if rollback required"
    )
