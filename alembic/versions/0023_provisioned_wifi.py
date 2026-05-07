"""Add wifi_ssid and wifi_psk columns to provisioned_images.

Carries the per-build WiFi credentials chosen by the operator. Both
columns are nullable; the build endpoint enforces that they are either
both set or both NULL. They are NOT cleared on terminal success
(unlike ``fleet_env_payload``) -- the Built Images UI surfaces the
SSID/PSK in a tooltip so the operator can recover credentials baked
into a downloaded image.

Revision ID: 0023
Revises: 0022
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "provisioned_images",
        sa.Column("wifi_ssid", sa.Text(), nullable=True),
    )
    op.add_column(
        "provisioned_images",
        sa.Column("wifi_psk", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    # Project policy (test_migration_policy.py): every downgrade must
    # raise NotImplementedError. Real downgrades are dangerous in
    # production rollback (data loss / FK ordering / re-upgrade
    # back-fill) and the project has chosen to invest in forward
    # migrations only.
    raise NotImplementedError(
        "Downgrade of 0023 is intentionally not implemented per project policy."
    )
