"""jobs: add progress_stage / progress_pct columns

The imager build + import flows take several minutes, and today the UI
polls a job that only ever transitions PENDING -> PROCESSING -> DONE,
so the user sees a silent spinner for the whole pipeline.  These two
columns let the worker emit coarse stage labels (e.g. ``downloading``,
``building``, ``uploading``) and an optional 0-100 percent estimate
that the UI can render under the status badge.

Both columns are additive and nullable / defaulted, so deploys are
safe in either order: an old worker leaves them empty (UI falls back
to the existing badge); a new worker writes to them and an old API is
unaffected because the columns aren't selected.

Revision ID: 0020
Revises: 0019
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "jobs",
        sa.Column(
            "progress_stage",
            sa.Text(),
            nullable=False,
            server_default="",
        ),
    )
    op.add_column(
        "jobs",
        sa.Column("progress_pct", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "downgrade not implemented; restore from backup if rollback required"
    )
