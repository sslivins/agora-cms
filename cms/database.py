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

import logging
from pathlib import Path

from sqlalchemy import inspect as sa_inspect

from shared.database import Base, init_db, get_db, dispose_db, create_tables  # noqa: F401
from shared.database import get_engine, get_session_factory, wait_for_db  # noqa: F401
from shared import database as _shared_db

logger = logging.getLogger("agora.database.migrations")


# Repo root — resolved relative to this file so the code works regardless
# of CWD (docker-compose, uvicorn, pytest, cloud runners).
_REPO_ROOT = Path(__file__).resolve().parent.parent
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def _alembic_config():
    """Build an Alembic Config pointed at this repo's alembic.ini.

    We don't override ``sqlalchemy.url`` here — ``alembic/env.py`` resolves
    the URL from ``SharedSettings`` (the same source the running app uses),
    which keeps the CLI and the startup path on exactly one code path.

    ``alembic`` is imported lazily here so non-CMS images (e.g. the worker
    image, which imports CMS ORM models via ``cms.models`` to share
    ``Base.metadata`` but never runs migrations) don't have to pip-install
    alembic just to keep this module importable.
    """
    from alembic.config import Config as AlembicConfig  # local import

    return AlembicConfig(str(_ALEMBIC_INI))


def _run_alembic_to_head(sync_connection, cfg, op: str) -> None:
    """Run an Alembic command against an already-open (sync) Connection.

    Designed to be called via ``AsyncConnection.run_sync`` so that Alembic's
    ``env.py`` reuses this connection (passed through
    ``cfg.attributes['connection']``) instead of constructing its own engine.
    ``op`` is ``"upgrade"`` or ``"stamp"``; both target ``head``.
    """
    from alembic import command as alembic_command  # local import

    cfg.attributes["connection"] = sync_connection
    if op == "stamp":
        alembic_command.stamp(cfg, "head")
    else:
        alembic_command.upgrade(cfg, "head")


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

    # We run Alembic on the application's *primary* engine connection (via
    # ``run_sync`` + ``config.attributes['connection']``; see ``alembic/env.py``)
    # rather than letting env.py build its own second async engine.  That
    # second engine's connect has been observed to hang indefinitely on
    # freshly scheduled Azure Container Apps replicas, wedging revision
    # activation.  The primary engine is already proven reachable here.
    from alembic.script import ScriptDirectory  # lazy: see _alembic_config

    cfg = _alembic_config()

    # Inspect the DB on the application's *primary* engine — the same one
    # the app already proved it can reach via ``wait_for_db()``.  While we
    # hold that connection, also read the current Alembic revision(s) so we
    # can short-circuit the no-op case below.
    async with _shared_db._engine.begin() as conn:
        def _inspect(c):
            insp = sa_inspect(c)
            has_alembic = insp.has_table("alembic_version")
            has_legacy = insp.has_table("assets")
            revs: list[str] = []
            if has_alembic:
                res = c.exec_driver_sql("SELECT version_num FROM alembic_version")
                revs = [row[0] for row in res.fetchall()]
            return has_alembic, has_legacy, revs

        has_alembic, has_legacy_assets, current_rev_list = await conn.run_sync(
            _inspect
        )

    current_revs = set(current_rev_list)

    # Fast path: the DB is already at head, so ``alembic upgrade head`` would
    # be a no-op.  Skip it entirely — no need to import/run Alembic's env at
    # all on a healthy boot.  (When there IS pending work, the upgrade below
    # runs on the primary connection, so it no longer risks the second-engine
    # hang that historically wedged ACA revision activation.)
    if has_alembic and current_revs:
        heads = set(ScriptDirectory.from_config(cfg).get_heads())
        if current_revs == heads:
            logger.info(
                "Database already at head (%s); skipping alembic upgrade.",
                ", ".join(sorted(current_revs)),
            )
            return
        logger.info(
            "Database revisions %s differ from alembic heads %s; running upgrade.",
            sorted(current_revs),
            sorted(heads),
        )

    if not has_alembic and has_legacy_assets:
        logger.info(
            "Legacy pre-Alembic schema detected; stamping as baseline "
            "without running DDL."
        )
        async with _shared_db._engine.connect() as conn:
            await conn.run_sync(_run_alembic_to_head, cfg, "stamp")
        return

    logger.info("Running alembic upgrade head")
    async with _shared_db._engine.connect() as conn:
        await conn.run_sync(_run_alembic_to_head, cfg, "upgrade")
