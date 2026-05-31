"""Alembic migration 0036 — friendly-name snapshot for approval cards.

Adds ``display_arguments`` (JSONB on Postgres, JSON on SQLite) to
``chat_pending_approvals``.  See
:mod:`cms.services.assistant.approval_display` for the resolver that
populates it.

The column is nullable on purpose:

* **Read-tool turns** never create an approval row, so the column is
  irrelevant for them.
* **Legacy rows** (anything written before this migration ran) stay
  NULL — the frontend already had to handle that case (since the
  resolver may also return an empty mapping if no IDs in the args
  could be resolved).
* **Resolver failures** (transient DB hiccup, unknown ID schema)
  store NULL rather than blocking the approval card.

Revision ID: 0036
Revises: 0035
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0036"
down_revision = "0035"
branch_labels = None
depends_on = None


def _json_type(bind):
    dialect = bind.dialect.name
    if dialect == "postgresql":
        return sa.dialects.postgresql.JSONB()
    return sa.JSON()


def upgrade() -> None:
    bind = op.get_bind()
    op.add_column(
        "chat_pending_approvals",
        sa.Column("display_arguments", _json_type(bind), nullable=True),
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0036 is not supported")
