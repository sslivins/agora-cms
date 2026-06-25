"""assets: user-editable description column + trigram search index

Adds a nullable free-text ``description`` column to ``assets`` plus a
trigram GIN index (Postgres only) mirroring the existing
``display_name`` / ``filename`` search indexes from migration 0030.

The asset library's ``q`` substring search (``GET /api/assets/page``)
now matches against ``description`` in addition to the name fields, so
users can find an asset by notes they've written about it.

As in 0030, ``pg_trgm`` is enabled best-effort inside a savepoint: if
the extension isn't allow-listed (typical on Azure PG Flex), the index
is skipped and ``LIKE`` falls back to a sequential scan. SQLite (CI
unit-test matrix) doesn't support GIN/pg_trgm so the index is skipped
there too; the column itself is created on every dialect.

Revision ID: 0051
Revises: 0050
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op

revision = "0051"
down_revision = "0050"
branch_labels = None
depends_on = None


log = logging.getLogger("alembic.runtime.migration")


def _try_enable_pg_trgm(bind) -> bool:
    """Best-effort CREATE EXTENSION pg_trgm (see migration 0030)."""
    sp = bind.begin_nested()
    try:
        bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        sp.commit()
        return True
    except Exception as exc:  # pragma: no cover -- exercised on Azure only
        sp.rollback()
        log.warning(
            "pg_trgm extension unavailable (%s); skipping description "
            "trigram index. Allow-list pg_trgm via azure.extensions and "
            "create the index manually to enable accelerated LIKE search.",
            exc,
        )
        return False


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    op.add_column(
        "assets",
        sa.Column("description", sa.Text(), nullable=True),
    )

    if dialect == "postgresql":
        if _try_enable_pg_trgm(bind):
            op.execute(
                "CREATE INDEX IF NOT EXISTS idx_assets_description_trgm "
                "ON assets USING GIN (description gin_trgm_ops)"
            )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0051 is not supported")
