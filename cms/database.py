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

        # -- media metadata columns on assets --
        for col, col_type in [
            ("width", "INTEGER"),
            ("height", "INTEGER"),
            ("duration_seconds", "DOUBLE PRECISION"),
            ("video_codec", "VARCHAR(64)"),
            ("audio_codec", "VARCHAR(64)"),
            ("bitrate", "INTEGER"),
            ("frame_rate", "VARCHAR(16)"),
            ("color_space", "VARCHAR(64)"),
        ]:
            has_col = await conn.run_sync(lambda c, _c=col: _has_column(c, "assets", _c))
            if not has_col:
                await conn.execute(text(f"ALTER TABLE assets ADD COLUMN {col} {col_type}"))

        # -- media metadata columns on asset_variants --
        for col, col_type in [
            ("width", "INTEGER"),
            ("height", "INTEGER"),
            ("duration_seconds", "DOUBLE PRECISION"),
            ("video_codec", "VARCHAR(64)"),
            ("audio_codec", "VARCHAR(64)"),
            ("bitrate", "INTEGER"),
            ("frame_rate", "VARCHAR(16)"),
            ("color_space", "VARCHAR(64)"),
        ]:
            has_col = await conn.run_sync(lambda c, _c=col: _has_column(c, "asset_variants", _c))
            if not has_col:
                await conn.execute(text(f"ALTER TABLE asset_variants ADD COLUMN {col} {col_type}"))

        # -- schedules.loop_count --
        has_loop_count = await conn.run_sync(lambda c: _has_column(c, "schedules", "loop_count"))
        if not has_loop_count:
            await conn.execute(text("ALTER TABLE schedules ADD COLUMN loop_count INTEGER"))

        # -- Rename device status enum: approved → adopted, offline → orphaned --
        # Guard: only run if the enum type exists (skip on fresh databases)
        enum_exists = await conn.execute(
            text("SELECT 1 FROM pg_type WHERE typname = 'devicestatus'")
        )
        if enum_exists.scalar():
            has_approved = await conn.execute(
                text("SELECT 1 FROM pg_enum WHERE enumlabel = 'APPROVED' AND enumtypid = 'devicestatus'::regtype")
            )
            if has_approved.scalar():
                await conn.execute(text("ALTER TYPE devicestatus RENAME VALUE 'APPROVED' TO 'ADOPTED'"))
            has_offline = await conn.execute(
                text("SELECT 1 FROM pg_enum WHERE enumlabel = 'OFFLINE' AND enumtypid = 'devicestatus'::regtype")
            )
            if has_offline.scalar():
                await conn.execute(text("ALTER TYPE devicestatus RENAME VALUE 'OFFLINE' TO 'ORPHANED'"))


        # -- device_profiles.pixel_format and color_space --
        for col, col_type, default in [
            ("pixel_format", "VARCHAR(20)", "auto"),
            ("color_space", "VARCHAR(20)", "auto"),
        ]:
            has_col = await conn.run_sync(lambda c, _c=col: _has_column(c, "device_profiles", _c))
            if not has_col:
                await conn.execute(text(
                    f"ALTER TABLE device_profiles ADD COLUMN {col} {col_type} DEFAULT '{default}'"
                ))

        # -- devices.timezone --
        has_tz = await conn.run_sync(lambda c: _has_column(c, "devices", "timezone"))
        if not has_tz:
            await conn.execute(text(
                "ALTER TABLE devices ADD COLUMN timezone VARCHAR(64)"
            ))

    # Let create_all handle brand-new tables (device_profiles, asset_variants)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
