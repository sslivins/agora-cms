"""Capture row counts for every user table to a JSON file.

Used by the ``migration-safety`` workflow to detect destructive forward
migrations: we snapshot row counts on the seeded baseline database
*before* running the PR branch's migrations, then verify-post-migration
fails if any still-existing table has fewer rows than it did.

Silent data loss — e.g. a migration that drops a column without a
backfill, or one that rewrites a table and loses rows in the process —
doesn't show up in the existing verify (which only checks REQUIRED
tables are non-empty). Row-count comparison catches wholesale deletes
and replace-table-with-empty patterns.

Usage:
    python scripts/ci_capture_row_counts.py <output.json>

Writes ``{"tablename": N, ...}`` for every non-internal table.
"""

from __future__ import annotations

import asyncio
import json
import sys

from sqlalchemy import text

from cms.auth import get_settings
from cms.database import dispose_db, init_db, wait_for_db
from shared import database as _shared_db


async def capture(out_path: str) -> int:
    init_db(get_settings())
    await wait_for_db()

    counts: dict[str, int] = {}
    async with _shared_db._engine.connect() as conn:
        res = await conn.execute(text(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = 'public' "
            "  AND table_type = 'BASE TABLE' "
            "  AND table_name NOT LIKE 'alembic_%' "
            "ORDER BY table_name"
        ))
        tables = [r[0] for r in res.fetchall()]

        for t in tables:
            res = await conn.execute(text(f'SELECT COUNT(*) FROM "{t}"'))
            counts[t] = int(res.scalar() or 0)

    await dispose_db()

    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(counts, f, indent=2, sort_keys=True)

    print(f"Captured row counts for {len(counts)} tables → {out_path}")
    for t, n in sorted(counts.items()):
        print(f"  {t}: {n}")
    return 0


if __name__ == "__main__":
    if len(sys.argv) != 2:
        print("usage: ci_capture_row_counts.py <output.json>", file=sys.stderr)
        sys.exit(2)
    sys.exit(asyncio.run(capture(sys.argv[1])))
