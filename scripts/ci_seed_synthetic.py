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

from cms.auth import get_settings
from cms.database import dispose_db, init_db, run_migrations, wait_for_db
from shared import database as _shared_db


async def _table_info(conn, table: str) -> dict[str, dict]:
    """Return {column_name: {nullable, has_default, data_type}} for `table`."""
    rows = await conn.execute(
        text(
            "SELECT column_name, is_nullable, column_default, data_type "
            "FROM information_schema.columns WHERE table_name = :t"
        ),
        {"t": table},
    )
    return {
        r[0]: {"nullable": r[1] == "YES", "has_default": r[2] is not None, "data_type": r[3]}
        for r in rows
    }


async def _fk_targets(conn, table: str) -> dict[str, str]:
    """Return {column_name: referenced_table} for FKs on `table`."""
    rows = await conn.execute(text(
        "SELECT kcu.column_name, ccu.table_name "
        "FROM information_schema.table_constraints tc "
        "JOIN information_schema.key_column_usage kcu "
        "  ON tc.constraint_name = kcu.constraint_name "
        " AND tc.table_schema = kcu.table_schema "
        "JOIN information_schema.constraint_column_usage ccu "
        "  ON ccu.constraint_name = tc.constraint_name "
        " AND ccu.table_schema = tc.table_schema "
        "WHERE tc.constraint_type = 'FOREIGN KEY' "
        "  AND tc.table_name = :t"
    ), {"t": table})
    return {r[0]: r[1] for r in rows}


async def _any_existing_pk(conn, table: str):
    """Return one PK value from `table`, or None if empty."""
    try:
        res = await conn.execute(text(f"SELECT id FROM {table} ORDER BY 1 LIMIT 1"))
        return res.scalar()
    except Exception:
        return None


def _placeholder_for(col: str, info: dict, idx: int):
    """Best-effort value for a NOT NULL column the seed didn't supply."""
    dtype = (info.get("data_type") or "").lower()
    if "char" in dtype or "text" in dtype:
        return f"seed_{col}_{idx}"
    if "int" in dtype:
        return idx
    if "bool" in dtype:
        return False
    if "json" in dtype:
        return "{}"
    if "timestamp" in dtype or "date" in dtype:
        return datetime.now(timezone.utc)
    if "uuid" in dtype:
        return uuid.uuid4()
    if "double" in dtype or "real" in dtype or "numeric" in dtype:
        return 0
    return f"seed_{col}_{idx}"


async def _existing_columns(conn, table: str) -> set[str]:
    info = await _table_info(conn, table)
    return set(info.keys())


_seed_counter = {"i": 0}


async def _insert(conn, table: str, row: dict) -> None:
    """Insert `row` into `table`.

    - Filters out keys for columns the table doesn't have (schema drift).
    - Auto-fills any NOT NULL column without a default that the caller
      didn't supply, so the seed survives schema additions in `main`
      that happen between when this script was last updated and now.
    - For required FK columns, picks an existing PK from the referenced
      table; if the referenced table is empty, the row is skipped.
    """
    info = await _table_info(conn, table)
    if not info:
        return
    fks = await _fk_targets(conn, table)
    filtered = {k: v for k, v in row.items() if k in info}
    _seed_counter["i"] += 1
    idx = _seed_counter["i"]
    for col, meta in info.items():
        if col in filtered:
            continue
        if meta["nullable"] or meta["has_default"]:
            continue
        if col in fks:
            ref = await _any_existing_pk(conn, fks[col])
            if ref is None:
                # Required FK target is empty — skip this row entirely
                # rather than crash the whole seed.
                return
            filtered[col] = ref
        else:
            filtered[col] = _placeholder_for(col, meta, idx)
    if not filtered:
        return
    keys = ", ".join(filtered.keys())
    placeholders = ", ".join(f":{k}" for k in filtered)
    await conn.execute(
        text(f"INSERT INTO {table} ({keys}) VALUES ({placeholders})"),
        filtered,
    )


async def seed(count: int = 10) -> None:
    init_db(get_settings())
    await wait_for_db()
    await run_migrations()

    async with _shared_db._engine.begin() as conn:
        now = datetime.now(timezone.utc)

        # roles (must exist before users — users.role_id FK)
        for name in ("admin", "editor", "viewer"):
            await _insert(conn, "roles", {
                "id": uuid.uuid4(),
                "name": name,
                "description": f"seed {name} role",
                "created_at": now,
            })

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
