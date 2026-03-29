"""Database engine and session management."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from cms.config import Settings


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory = None


def init_db(settings: Settings):
    global _engine, _session_factory
    _engine = create_async_engine(settings.database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def dispose_db():
    global _engine
    if _engine:
        await _engine.dispose()


async def get_db() -> AsyncSession:
    async with _session_factory() as session:
        yield session


async def create_tables():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def run_migrations():
    """Apply incremental schema changes that create_all won't handle.

    create_all only creates new tables; it won't add columns to existing ones.
    This function adds missing columns/tables manually.
    """
    from sqlalchemy import text, inspect as sa_inspect

    async with _engine.begin() as conn:
        # Helper to check if a column exists in a table
        def _has_column(connection, table_name, column_name):
            insp = sa_inspect(connection)
            if not insp.has_table(table_name):
                return True  # table doesn't exist; create_all will handle it
            columns = [c["name"] for c in insp.get_columns(table_name)]
            return column_name in columns

        # -- devices.profile_id --
        has_profile_id = await conn.run_sync(lambda c: _has_column(c, "devices", "profile_id"))
        if not has_profile_id:
            await conn.execute(text(
                "ALTER TABLE devices ADD COLUMN profile_id UUID "
                "REFERENCES device_profiles(id) ON DELETE SET NULL"
            ))

        # -- assets.original_filename --
        has_orig = await conn.run_sync(lambda c: _has_column(c, "assets", "original_filename"))
        if not has_orig:
            await conn.execute(text(
                "ALTER TABLE assets ADD COLUMN original_filename VARCHAR(255)"
            ))

    # Let create_all handle brand-new tables (device_profiles, asset_variants)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
