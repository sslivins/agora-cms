"""Alembic environment for agora-cms.

The engine URL is pulled from ``SharedSettings.database_url`` so the same
configuration that powers the running application also drives migrations.
This avoids the ``alembic.ini`` drift trap (sqlalchemy.url is just a stub
placeholder there, so the CLI still works from a checkout).

All ORM models are imported here — both CMS-side and shared-side — so
Alembic's autogenerate sees the complete metadata.  Missing imports would
silently drop tables from migrations, which is exactly the failure mode
we're trying to eliminate.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# Register all models with Base.metadata so autogenerate sees them.
# Order matters only to the extent that a side-effectful import must
# happen before we reference Base.metadata below.
from shared.database import Base  # noqa: E402
import shared.models  # noqa: F401,E402 — registers shared-side tables
import cms.models  # noqa: F401,E402 — registers CMS-side tables

from shared.config import SharedSettings  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    """Resolve the runtime DB URL.

    Precedence (highest first):
      1. ``-x sqlalchemy.url=...`` from the alembic CLI.
      2. ``AGORA_CMS_DATABASE_URL`` via SharedSettings.
      3. ``sqlalchemy.url`` in alembic.ini (a local-dev stub).
    """
    x_args = context.get_x_argument(as_dictionary=True)
    if "sqlalchemy.url" in x_args:
        return x_args["sqlalchemy.url"]
    settings = SharedSettings()
    return settings.database_url


def run_migrations_offline() -> None:
    """Generate SQL without connecting to a DB (for `alembic upgrade --sql`)."""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    cfg_section = config.get_section(config.config_ini_section, {})
    cfg_section["sqlalchemy.url"] = _database_url()

    connectable = async_engine_from_config(
        cfg_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
