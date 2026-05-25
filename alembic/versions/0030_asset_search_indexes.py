"""trigram + uploaded_at indexes for asset library search and pagination

Phase 1 of the Asset Library enhancements (see session plan
"Asset Library Phase 1"):

- Enables ``pg_trgm`` (Postgres only) so substring search across
  ``display_name``, ``original_filename``, and ``filename`` can use a
  GIN index rather than a sequential scan.  On Azure Postgres Flexible
  Server ``pg_trgm`` must be in the ``azure.extensions`` allow-list
  server parameter; if it isn't, ``CREATE EXTENSION`` fails with a
  permission error.  This migration treats that as a soft failure: the
  trigram indexes are skipped, ``LIKE`` falls back to a sequential
  scan (acceptable on the current asset volume), and an operator can
  allow-list pg_trgm and ``CREATE INDEX ... USING gin (... gin_trgm_ops)``
  manually later without needing another migration.
- Adds a partial btree on ``uploaded_at DESC WHERE deleted_at IS NULL``
  to cover the common newest-first ordering of the visible asset set.

SQLite (used in CI for the unit-test matrix) doesn't support
``pg_trgm`` or GIN.  The trigram indexes are skipped on SQLite and the
``LIKE`` queries fall back to a sequential scan there — fine for the
small synthetic datasets the tests use, and the production target is
always Postgres anyway.

Revision ID: 0030
Revises: 0029
"""

from __future__ import annotations

import logging

import sqlalchemy as sa
from alembic import op


revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


log = logging.getLogger("alembic.runtime.migration")


def _try_enable_pg_trgm(bind) -> bool:
    """Best-effort CREATE EXTENSION pg_trgm.

    Uses a savepoint so a permission failure (typical on Azure PG Flex
    when the extension isn't allow-listed) doesn't poison the outer
    migration transaction. Returns True on success, False otherwise.
    """
    sp = bind.begin_nested()
    try:
        bind.execute(sa.text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
        sp.commit()
        return True
    except Exception as exc:  # pragma: no cover -- exercised on Azure only
        sp.rollback()
        log.warning(
            "pg_trgm extension unavailable (%s); skipping trigram indexes. "
            "Allow-list pg_trgm via azure.extensions server parameter and "
            "create the indexes manually to enable accelerated LIKE search.",
            exc,
        )
        return False


def upgrade() -> None:
    bind = op.get_bind()
    dialect = bind.dialect.name

    if dialect == "postgresql":
        if _try_enable_pg_trgm(bind):
            op.execute(
                "CREATE INDEX IF NOT EXISTS idx_assets_display_name_trgm "
                "ON assets USING GIN (display_name gin_trgm_ops)"
            )
            op.execute(
                "CREATE INDEX IF NOT EXISTS idx_assets_original_filename_trgm "
                "ON assets USING GIN (original_filename gin_trgm_ops)"
            )
            op.execute(
                "CREATE INDEX IF NOT EXISTS idx_assets_filename_trgm "
                "ON assets USING GIN (filename gin_trgm_ops)"
            )

    # Plain btree on uploaded_at — Postgres and SQLite can both scan it
    # in reverse to satisfy ``ORDER BY uploaded_at DESC``. Partial on
    # ``deleted_at IS NULL`` because every list-asset query filters that.
    op.create_index(
        "idx_assets_uploaded_at_live",
        "assets",
        ["uploaded_at"],
        postgresql_where="deleted_at IS NULL",
        sqlite_where="deleted_at IS NULL",
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0030 is not supported")
