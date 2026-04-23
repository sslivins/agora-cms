"""device_alert_state + devices.upgrade_started_at

Stage 4 of the multi-replica rollout (issue #344).  Adds two pieces of
shared state so alert handling and upgrade claim-ownership survive
under N>1 CMS replicas:

* ``devices.upgrade_started_at`` — timestamp-as-claim token replacing
  the in-memory ``_upgrading`` set in ``cms/routers/devices.py``.  The
  upgrade endpoint atomically CAS-claims by setting this column; the
  TTL-on-read check lets a failed claim self-heal without a sweeper.
* ``device_alert_state`` — per-device persisted alert state consumed
  by the rewritten ``cms.services.alert_service`` loop.  Replaces the
  in-memory ``_offline_timers`` / ``_was_offline`` dicts so alerting
  is consistent across replicas.

Revision ID: 0007_alert_state_upgrade_claim
Revises: 0006_leader_leases
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "upgrade_started_at", sa.DateTime(timezone=True), nullable=True
        ),
    )
    op.create_table(
        "device_alert_state",
        sa.Column(
            "device_id",
            sa.String(length=64),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column(
            "offline_since", sa.DateTime(timezone=True), nullable=True
        ),
        sa.Column(
            "offline_notified",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_index(
        "ix_device_alert_state_pending",
        "device_alert_state",
        ["offline_since"],
        postgresql_where=sa.text("offline_notified = false"),
    )


def downgrade() -> None:
    raise NotImplementedError("Downgrade is not supported for this migration.")
