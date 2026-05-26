"""Add ``purpose`` column to ``device_profiles``.

Phase 2 of the Asset Library enhancements: a new built-in profile
``thumbnail`` (purpose=``thumbnail``) generates tiny JPEG stills used
by the asset library grid view.  Existing rows default to
``purpose='device'``.

Revision ID: 0031
Revises: 0030
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "device_profiles",
        sa.Column(
            "purpose",
            sa.String(length=20),
            nullable=False,
            server_default="device",
        ),
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0031 is not supported")
