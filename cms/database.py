"""Database engine, session management, and migration entry point.

Re-exports shared database primitives and drives Alembic migrations on
startup.  Schema evolution is managed by Alembic revisions under
``alembic/versions/`` — there are no hand-written ALTER TABLE blocks in
this file any more.  If you need a schema change, generate a new
revision:

    alembic revision --autogenerate -m "<short description>"

and commit it alongside the model change.  The baseline revision
(``0001_baseline.py``) represents the schema as it stood when Alembic
was adopted.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from alembic import command as alembic_command
from alembic.config import Config as AlembicConfig
from sqlalchemy import inspect as sa_inspect

from shared.database import Base, init_db, get_db, dispose_db, create_tables  # noqa: F401
from shared.database import get_engine, get_session_factory, wait_for_db  # noqa: F401
from shared import database as _shared_db

logger = logging.getLogger("agora.database.migrations")


# Repo root — resolved relative to this file so the code works regardless
# of CWD (docker-compose, uvicorn, pytest, cloud runners).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def _alembic_config() -> AlembicConfig:
    """Build an Alembic Config pointed at this repo's alembic.ini.

    We don't override ``sqlalchemy.url`` here — ``alembic/env.py`` resolves
    the URL from ``SharedSettings`` (the same source the running app uses),
    which keeps the CLI and the startup path on exactly one code path.
    """
    return AlembicConfig(str(_ALEMBIC_INI))


async def run_migrations() -> None:
    """Bring the database schema up to date with the latest Alembic revision.

    Behaviour is decided by the state of the DB at call time:

    * **Managed DB** (``alembic_version`` table present): run
      ``alembic upgrade head``.  This is a no-op when already at head and
      applies any pending revisions otherwise.
    * **Legacy DB** (no ``alembic_version`` but an ``assets`` table from
      the old hand-written-DDL era): ``alembic stamp head`` to mark the
      existing schema as matching the baseline revision.  No DDL runs.
      On the next boot the DB will take the managed path.
    * **Fresh DB** (neither marker present): ``alembic upgrade head``.
      The baseline revision creates every table from scratch.

    This function is idempotent — calling it repeatedly on an up-to-date
    DB is safe.
    """

    async with _shared_db._engine.begin() as conn:
        has_alembic = await conn.run_sync(
            lambda c: sa_inspect(c).has_table("alembic_version")
        )
        has_legacy_assets = await conn.run_sync(
            lambda c: sa_inspect(c).has_table("assets")
        )

    # Alembic's env.py sets up its own async engine from SharedSettings,
    # so all we pass through is the Config pointing at alembic.ini.
    #
    # alembic.command.upgrade/stamp are synchronous and — via our env.py —
    # internally call ``asyncio.run()``.  We can't call asyncio.run from
    # inside a running event loop, so we hand off to a worker thread,
    # which gets its own loop.
    cfg = _alembic_config()

    if not has_alembic and has_legacy_assets:
        logger.info(
            "Legacy pre-Alembic schema detected; stamping as baseline "
            "without running DDL."
        )
        await asyncio.to_thread(alembic_command.stamp, cfg, "head")
        return

    logger.info("Running alembic upgrade head")
    await asyncio.to_thread(alembic_command.upgrade, cfg, "head")
