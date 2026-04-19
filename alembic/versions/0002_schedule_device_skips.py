"""per-device schedule skips

Revision ID: 0002
Revises: 0001
Create Date: 2026-04-20

"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = '0002'
down_revision: Union[str, None] = '0001'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'schedule_device_skips',
        sa.Column('schedule_id', sa.UUID(), nullable=False),
        sa.Column('device_id', sa.String(length=64), nullable=False),
        sa.Column('skip_until', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ['schedule_id'], ['schedules.id'], ondelete='CASCADE',
        ),
        sa.ForeignKeyConstraint(
            ['device_id'], ['devices.id'], ondelete='CASCADE',
        ),
        sa.PrimaryKeyConstraint('schedule_id', 'device_id'),
    )


def downgrade() -> None:
    op.drop_table('schedule_device_skips')
