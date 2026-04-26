"""devices.capabilities column for firmware-advertised feature flags

Slideshow asset support (issue: slideshow/v1).  Devices announce the
features their firmware implements via a JSON list of capability strings
in the REGISTER handshake.  The CMS persists the most recently reported
list and uses it to gate features that require new firmware behaviour
(e.g. ``slideshow_v1``: device can render a list of slides resolved
inline in a ``FETCH_ASSET`` message).

Older firmware versions register without the ``capabilities`` field, so
the column defaults to ``[]`` (empty list).  Feature gates treat an
empty/missing capability list as "incompatible" and refuse to schedule
slideshow assets onto groups containing such devices.

JSON portability mirrors ``devices.display_ports`` (migration 0014):
``sa.JSON`` resolves to ``JSONB`` on Postgres and ``TEXT`` on SQLite.

Revision ID: 0016
Revises: 0015
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "capabilities",
            sa.JSON(),
            nullable=False,
            server_default=sa.text("'[]'"),
        ),
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0016 is not supported")
