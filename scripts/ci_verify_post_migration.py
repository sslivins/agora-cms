"""Post-migration verification for the migration-safety CI job.

Asserts that the database is still queryable after a fresh round of
``run_migrations`` against a populated dataset:

- every expected table exists
- row counts are non-zero (seeded data wasn't accidentally wiped)
- a couple of typical relational queries still execute without error
"""

from __future__ import annotations

import asyncio
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


async def verify() -> int:
    init_db(get_settings())
    await wait_for_db()
    await run_migrations()

    failures: list[str] = []

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
                "FROM assets a LEFT JOIN asset_variants v ON v.asset_id = a.id "
                "GROUP BY a.id LIMIT 5"
            ))
            rows = res.fetchall()
            print(f"  assets⋈variants: {len(rows)} sample rows")
        except Exception as exc:
            failures.append(f"assets⋈variants query failed: {exc}")

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
    sys.exit(asyncio.run(verify()))
