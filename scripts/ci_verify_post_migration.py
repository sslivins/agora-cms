"""Post-migration verification for the migration-safety CI job.

Asserts that the database is still queryable after a fresh round of
``run_migrations`` against a populated dataset:

- every expected table exists
- row counts are non-zero (seeded data wasn't accidentally wiped)
- a couple of typical relational queries still execute without error
"""

from __future__ import annotations

import asyncio
import json
import os
import sys

from sqlalchemy import text

from cms.auth import get_settings
from cms.database import dispose_db, init_db, run_migrations, wait_for_db
from shared import database as _shared_db


EXPECTED_TABLES = [
    "users",
    "device_groups",
    "device_profiles",
    "devices",
    "assets",
    "asset_variants",
    "schedules",
    "api_keys",
]


async def verify(baseline_counts_path: str | None = None) -> int:
    init_db(get_settings())
    await wait_for_db()
    await run_migrations()

    failures: list[str] = []

    baseline_counts: dict[str, int] = {}
    if baseline_counts_path and os.path.exists(baseline_counts_path):
        with open(baseline_counts_path, "r", encoding="utf-8") as f:
            baseline_counts = json.load(f)
        print(f"Loaded baseline row counts for {len(baseline_counts)} tables")

    # Tables we have high confidence will be seeded — empty == catastrophe.
    REQUIRED_NONEMPTY = {"users", "devices", "assets"}

    async with _shared_db._engine.connect() as conn:
        # 1. Every expected table exists; required ones are non-empty.
        for t in EXPECTED_TABLES:
            try:
                res = await conn.execute(text(f"SELECT COUNT(*) FROM {t}"))
                n = res.scalar() or 0
            except Exception as exc:
                failures.append(f"cannot query {t}: {exc}")
                continue
            if t in REQUIRED_NONEMPTY and n == 0:
                failures.append(f"{t} is empty after migration (seeded data lost)")
            print(f"  {t}: {n} rows")

        # 2. A relational sanity query — devices joined to groups & profiles.
        try:
            res = await conn.execute(text(
                "SELECT d.id, d.name, g.name, p.name "
                "FROM devices d "
                "LEFT JOIN device_groups g ON g.id = d.group_id "
                "LEFT JOIN device_profiles p ON p.id = d.profile_id "
                "LIMIT 5"
            ))
            rows = res.fetchall()
            print(f"  devices⋈groups⋈profiles: {len(rows)} sample rows")
        except Exception as exc:
            failures.append(f"devices JOIN query failed: {exc}")

        # 3. Asset→variant relationship still intact.
        try:
            res = await conn.execute(text(
                "SELECT a.id, COUNT(v.id) "
                "FROM assets a LEFT JOIN asset_variants v ON v.source_asset_id = a.id "
                "GROUP BY a.id LIMIT 5"
            ))
            rows = res.fetchall()
            print(f"  assets⋈variants: {len(rows)} sample rows")
        except Exception as exc:
            failures.append(f"assets⋈variants query failed: {exc}")

        # 4. Destructive-forward guard: for every table that existed in the
        #    baseline and STILL exists post-migration, row count must not
        #    decrease. Catches silent data loss (DROP + CREATE, bad copy,
        #    wholesale DELETE, etc.) that a schema-only check would miss.
        #    A migration that intentionally deletes rows should explicitly
        #    re-seed to the expected count in its own logic, or the seed
        #    script should be updated in lockstep.
        if baseline_counts:
            print("")
            print("Row-count preservation check:")
            res = await conn.execute(text(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = 'public' AND table_type = 'BASE TABLE'"
            ))
            current_tables = {r[0] for r in res.fetchall()}

            for t, baseline_n in sorted(baseline_counts.items()):
                if t not in current_tables:
                    # Dropping a table is an explicit schema change; not
                    # flagged here. Schema-level review catches it.
                    print(f"  {t}: DROPPED (baseline had {baseline_n})")
                    continue
                res = await conn.execute(text(f'SELECT COUNT(*) FROM "{t}"'))
                current_n = int(res.scalar() or 0)
                if current_n < baseline_n:
                    failures.append(
                        f"destructive migration: {t} row count dropped "
                        f"{baseline_n} → {current_n}"
                    )
                    print(f"  {t}: {baseline_n} → {current_n} ❌")
                else:
                    print(f"  {t}: {baseline_n} → {current_n}")

    await dispose_db()

    if failures:
        print("")
        print("❌ Migration-safety verification FAILED:")
        for f in failures:
            print(f"  - {f}")
        return 1

    print("✅ Migration-safety verification passed")
    return 0


if __name__ == "__main__":
    baseline = sys.argv[1] if len(sys.argv) > 1 else None
    sys.exit(asyncio.run(verify(baseline)))
