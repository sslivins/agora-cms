"""users: reset_token + reset_token_created_at for self-service password reset

Adds two nullable columns to ``users`` backing the forgot-password / reset
flow (issue #231):

* ``reset_token`` — a single-use, unique, time-limited secret emailed to the
  user (or minted by the ``reset-password`` CLI). The ``/reset-password``
  handler validates it, lets the user set a new password, then burns it.
* ``reset_token_created_at`` — when the token was issued, driving the
  ``RESET_TOKEN_TTL`` (1 hour) expiry check in ``reset_token_is_expired``.

No backfill: no reset tokens exist before this migration ships, so both
columns start NULL for every row. Adding nullable columns with no default is
a metadata-only change in Postgres (brief ACCESS EXCLUSIVE, no table rewrite).

Revision ID: 0053
Revises: 0052
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0053"
down_revision = "0052"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("reset_token", sa.String(length=128), nullable=True),
    )
    op.add_column(
        "users",
        sa.Column("reset_token_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_unique_constraint("uq_users_reset_token", "users", ["reset_token"])


def downgrade() -> None:
    # Project policy (tests/test_migration_policy.py): downgrades are not
    # supported — forward-only migrations.
    raise NotImplementedError("downgrade not supported")
