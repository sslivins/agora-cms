"""Integration test: ``run_migrations`` drives REAL alembic through the
application's primary connection (``cfg.attributes['connection']`` +
``AsyncConnection.run_sync``) instead of letting ``alembic/env.py`` build
its own second async engine.

The fake-based ``test_run_migrations_fast_path`` suite pins branch
selection but spies away ``alembic.command``; it never runs alembic's
``env.py``.  This test closes that gap by running the real alembic stamp
end-to-end against the test database and asserting the schema is marked at
head — proving the env.py injection branch is wired correctly.

The ``db_engine`` fixture builds the ORM schema via ``Base.metadata
.create_all`` but never creates ``alembic_version`` — i.e. the "legacy
schema" state — so ``run_migrations`` takes the real ``alembic stamp head``
path through the injected connection.
"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect as sa_inspect

import cms.database as dbmod


@pytest.mark.asyncio
async def test_run_migrations_stamps_via_injected_connection(db_engine, monkeypatch):
    monkeypatch.setattr(dbmod._shared_db, "_engine", db_engine)

    # Precondition: legacy state — tables exist, but no alembic_version yet.
    async with db_engine.connect() as conn:
        has_alembic_before = await conn.run_sync(
            lambda c: sa_inspect(c).has_table("alembic_version")
        )
    assert has_alembic_before is False

    # Runs REAL alembic stamp through env.py's injected-connection branch.
    await dbmod.run_migrations()

    cfg = dbmod._alembic_config()
    from alembic.script import ScriptDirectory

    expected_heads = set(ScriptDirectory.from_config(cfg).get_heads())

    async with db_engine.connect() as conn:
        def _read(c):
            insp = sa_inspect(c)
            assert insp.has_table("alembic_version")
            res = c.exec_driver_sql("SELECT version_num FROM alembic_version")
            return {row[0] for row in res.fetchall()}

        recorded = await conn.run_sync(_read)

    assert recorded == expected_heads
