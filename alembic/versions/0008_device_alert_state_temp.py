"""device_alert_state temperature columns (N=2 fix for temp alerts)

Issue #344 multi-replica audit — HIGH severity.

Adds the persisted temperature-alert state columns that back the
rewritten ``AlertService.check_temperature`` DB-CAS logic.  Without
these, two CMS replicas processing STATUS heartbeats for the same
device via Azure Web PubSub webhook routing will each maintain
independent ``_temp_states`` dicts and can double-fire temperature
alerts (or miss cooldowns that the other replica already applied).

The row is serialized via ``SELECT ... FOR UPDATE`` inside
``check_temperature``, so only one replica at a time can inspect-and-
mutate a given device's temp state.  The ``temp_last_sample_ts``
column also guards against out-of-order webhook delivery (an older
sample arriving after a newer one should be ignored).

Revision ID: 0008
Revises: 0007
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "device_alert_state",
        sa.Column(
            "temp_level",
            sa.String(length=16),
            nullable=False,
            server_default=sa.text("'normal'"),
        ),
    )
    op.add_column(
        "device_alert_state",
        sa.Column(
            "temp_last_alert_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "device_alert_state",
        sa.Column(
            "temp_last_sample_ts",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrade is not supported for this migration.")
