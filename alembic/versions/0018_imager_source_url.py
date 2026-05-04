"""imager: per-row catalog source URL + expected sha256

Adds two immutable fields to ``base_images`` populated at API enqueue
time so the worker (PR 3) does not have to re-fetch the upstream
catalog -- which is mutable -- to learn what bytes the admin actually
clicked on.  Without these columns, an upstream catalog rewrite
between API enqueue and worker pickup could silently swap the bytes
imported under the same ``(variant, version)`` key (TOCTOU).

Both columns are nullable for backward-compatibility with rows that
existed before this migration; PR 4's API endpoint will populate them
on insert.  The worker treats a NULL ``source_url`` / ``expected_sha256``
as a fall-back-to-catalog signal so PR 3 tests can run before PR 4
ships.

Revision ID: 0018
Revises: 0017
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "base_images",
        sa.Column("source_url", sa.Text(), nullable=True),
    )
    op.add_column(
        "base_images",
        sa.Column("expected_sha256", sa.Text(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("base_images", "expected_sha256")
    op.drop_column("base_images", "source_url")
