"""devices.display_ports column for per-HDMI-port state

Issue #350.  Firmware (sslivins/agora #117) reports per-port HDMI
connection state on every STATUS heartbeat as a ``display_ports``
array.  Today the CMS silently drops the field (Pydantic ``extra``
ignored at the schema, and there's no column to write to).  This
migration adds a JSON column so :func:`device_presence.update_status`
can persist the per-port list alongside the legacy ``display_connected``
flag.

JSON portability: ``sa.JSON`` resolves to ``JSONB`` on Postgres and
``TEXT`` on SQLite (with the JSON1 extension auto-loaded by aiosqlite),
matching the pattern already used for ``pending_registrations.connection_metadata``
in migration 0010.

Revision ID: 0014
Revises: 0013
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column("display_ports", sa.JSON(), nullable=True),
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0014 is not supported")
