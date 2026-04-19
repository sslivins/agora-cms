"""device_profiles.enabled

Revision ID: 0003
Revises: 0002
Create Date: 2026-04-19

Adds an `enabled` boolean to `device_profiles` (defaults to True).

Disabled profiles do not generate new variants on asset upload or
new-profile fan-out and have in-flight transcodes cancelled. See #237.

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0003'
down_revision: Union[str, None] = '0002'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'device_profiles',
        sa.Column(
            'enabled',
            sa.Boolean(),
            nullable=False,
            server_default=sa.text('true'),
        ),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Project policy: migrations are forward-only. "
        "Roll forward with a new migration if a schema correction is needed."
    )
