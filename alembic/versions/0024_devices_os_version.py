"""Add os_version column to devices.

Phase M4-CMS of the agora-os bundle-OTA migration.  Devices on
firmware ≥ M4-device report ``os_version`` in their register payload
(the agora-os bundle version, sourced from ``/etc/agora/version``).
``firmware_version`` continues to track the agora-app version.  This
column is what M5 will use as the dispatch comparison key once the
``/upgrade`` endpoint flips to ``os_update_dispatch``.

Indexed because the rollout UI will need to filter / count devices
by os_version (e.g. "how many devices are still on v0.0.16-test").

Revision ID: 0024
Revises: 0023
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "devices",
        sa.Column(
            "os_version",
            sa.String(length=32),
            nullable=False,
            server_default="",
        ),
    )
    op.create_index("ix_devices_os_version", "devices", ["os_version"])


def downgrade() -> None:
    # Project policy (test_migration_policy.py): every downgrade must
    # raise NotImplementedError. Real downgrades are dangerous in
    # production rollback (data loss / FK ordering / re-upgrade
    # back-fill) and the project has chosen to invest in forward
    # migrations only.
    raise NotImplementedError(
        "Downgrade of 0024 is intentionally not implemented per project policy."
    )
