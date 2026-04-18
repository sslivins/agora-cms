"""Seed a synthetic dataset for the migration-safety CI job.

Populates a handful of rows in every major table so that subsequent
migrations must contend with existing data, not an empty schema.

Intentionally uses raw SQL keyed off current column names queried at
runtime — so it stays robust as the schema evolves.  Columns that don't
exist yet are simply skipped (the migration that adds them will
still have to cope with the pre-existing rows once added).

Run against a database that already has the schema initialised
(via ``cms.database.run_migrations``).  Prints row counts on exit so
the CI log clearly shows the seeded volume.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from cms.database import dispose_db, init_db, run_migrations
from shared import database as _shared_db


async def _existing_columns(conn, table: str) -> set[str]:
    rows = await conn.execute(
        text(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = :t"
        ),
        {"t": table},
    )
    return {r[0] for r in rows}


async def _insert(conn, table: str, row: dict) -> None:
    """Insert `row` into `table`, filtering keys to columns that exist."""
    cols = await _existing_columns(conn, table)
    filtered = {k: v for k, v in row.items() if k in cols}
    if not filtered:
        return
    keys = ", ".join(filtered.keys())
    placeholders = ", ".join(f":{k}" for k in filtered)
    await conn.execute(
        text(f"INSERT INTO {table} ({keys}) VALUES ({placeholders})"),
        filtered,
    )


async def seed(count: int = 10) -> None:
    await init_db()
    await run_migrations()

    async with _shared_db._engine.begin() as conn:
        now = datetime.now(timezone.utc)

        # users
        user_ids = []
        for i in range(count):
            uid = uuid.uuid4()
            user_ids.append(uid)
            await _insert(conn, "users", {
                "id": uid,
                "username": f"seed_user_{i}",
                "email": f"seed_user_{i}@example.com",
                "password_hash": "$2b$12$abcdefghijklmnopqrstuv",
                "is_active": True,
                "created_at": now - timedelta(days=i),
            })

        # device_groups
        group_ids = []
        for i in range(count):
            gid = uuid.uuid4()
            group_ids.append(gid)
            await _insert(conn, "device_groups", {
                "id": gid,
                "name": f"seed_group_{i}",
                "description": f"seeded group #{i}",
                "created_at": now,
            })

        # device_profiles
        profile_ids = []
        for i in range(count):
            pid = uuid.uuid4()
            profile_ids.append(pid)
            await _insert(conn, "device_profiles", {
                "id": pid,
                "name": f"seed_profile_{i}",
                "video_codec": "h264",
                "audio_codec": "aac",
                "resolution": "1920x1080",
                "created_at": now,
            })

        # devices
        device_ids = []
        for i in range(count):
            did = uuid.uuid4()
            device_ids.append(did)
            await _insert(conn, "devices", {
                "id": did,
                "name": f"seed_device_{i}",
                "serial": f"SEEDPI{i:06d}",
                "group_id": group_ids[i % len(group_ids)] if group_ids else None,
                "profile_id": profile_ids[i % len(profile_ids)] if profile_ids else None,
                "status": "PENDING",
                "created_at": now,
                "last_seen_at": now - timedelta(minutes=i),
            })

        # assets
        asset_ids = []
        for i in range(count):
            aid = uuid.uuid4()
            asset_ids.append(aid)
            await _insert(conn, "assets", {
                "id": aid,
                "filename": f"seed_asset_{i}.mp4",
                "original_filename": f"seed_asset_{i}.mp4",
                "content_type": "video/mp4",
                "size_bytes": 1_000_000 + i,
                "status": "READY",
                "type": "VIDEO",
                "is_global": True,
                "created_at": now,
            })

        # asset_variants (multiple per asset)
        for i, aid in enumerate(asset_ids):
            for j in range(2):
                await _insert(conn, "asset_variants", {
                    "id": uuid.uuid4(),
                    "asset_id": aid,
                    "filename": f"seed_variant_{i}_{j}.mp4",
                    "profile_id": profile_ids[j % len(profile_ids)] if profile_ids else None,
                    "status": "READY",
                    "size_bytes": 500_000,
                    "created_at": now,
                })

        # schedules
        for i in range(count):
            await _insert(conn, "schedules", {
                "id": uuid.uuid4(),
                "name": f"seed_schedule_{i}",
                "asset_id": asset_ids[i % len(asset_ids)],
                "group_id": group_ids[i % len(group_ids)] if group_ids else None,
                "start_time": "08:00:00",
                "end_time": "18:00:00",
                "priority": 0,
                "enabled": True,
                "created_at": now,
            })

        # api_keys
        for i in range(count):
            await _insert(conn, "api_keys", {
                "id": uuid.uuid4(),
                "key_hash": f"seed_apikey_hash_{i:064d}",
                "name": f"seed_apikey_{i}",
                "user_id": user_ids[i % len(user_ids)] if user_ids else None,
                "created_at": now,
            })

        # Report row counts for log-visibility
        tables = [
            "users", "device_groups", "device_profiles", "devices",
            "assets", "asset_variants", "schedules", "api_keys",
        ]
        print("Seeded row counts:")
        for t in tables:
            res = await conn.execute(text(f"SELECT COUNT(*) FROM {t}"))
            print(f"  {t}: {res.scalar()}")

    await dispose_db()


if __name__ == "__main__":
    count = int(os.environ.get("SEED_COUNT", "10"))
    asyncio.run(seed(count))
