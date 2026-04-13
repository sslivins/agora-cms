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

    # Create any brand-new tables first so FK references in ALTER statements
    # can resolve (e.g. api_keys.user_id → users.id on first RBAC deploy).
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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

        # -- api_keys.user_id (RBAC) --
        has_user_id = await conn.run_sync(lambda c: _has_column(c, "api_keys", "user_id"))
        if not has_user_id:
            await conn.execute(text(
                "ALTER TABLE api_keys ADD COLUMN user_id UUID "
                "REFERENCES users(id) ON DELETE SET NULL"
            ))

        # -- assets.owner_group_id (RBAC) --
        has_owner = await conn.run_sync(lambda c: _has_column(c, "assets", "owner_group_id"))
        if not has_owner:
            await conn.execute(text(
                "ALTER TABLE assets ADD COLUMN owner_group_id UUID "
                "REFERENCES device_groups(id) ON DELETE SET NULL"
            ))

        # -- assets.is_global (RBAC asset scoping) --
        has_global = await conn.run_sync(lambda c: _has_column(c, "assets", "is_global"))
        if not has_global:
            await conn.execute(text(
                "ALTER TABLE assets ADD COLUMN is_global BOOLEAN DEFAULT false"
            ))
            # Mark existing assets as global for backward compatibility
            await conn.execute(text(
                "UPDATE assets SET is_global = true WHERE owner_group_id IS NULL"
            ))

        # -- assets.uploaded_by_user_id (track uploader for personal assets) --
        has_upby = await conn.run_sync(lambda c: _has_column(c, "assets", "uploaded_by_user_id"))
        if not has_upby:
            await conn.execute(text(
                "ALTER TABLE assets ADD COLUMN uploaded_by_user_id UUID "
                "REFERENCES users(id) ON DELETE SET NULL"
            ))

        # -- users.must_change_password (RBAC email login) --
        has_mcp = await conn.run_sync(lambda c: _has_column(c, "users", "must_change_password"))
        if not has_mcp:
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN must_change_password BOOLEAN DEFAULT false"
            ))

        # -- users.setup_token (one-time account setup link) --
        has_st = await conn.run_sync(lambda c: _has_column(c, "users", "setup_token"))
        if not has_st:
            await conn.execute(text(
                "ALTER TABLE users ADD COLUMN setup_token VARCHAR(128) UNIQUE"
            ))

        # -- users.email: set NOT NULL and backfill from username for legacy rows --
        # First backfill any NULL emails
        has_users = await conn.run_sync(lambda c: sa_inspect(c).has_table("users"))
        if has_users:
            await conn.execute(text(
                "UPDATE users SET email = username || '@localhost' WHERE email IS NULL"
            ))

        # -- Drop assets.owner_group_id (replaced by GroupAsset entries) --
        # Use explicit table+column check (can't rely on _has_column for drops)
        has_ogid = await conn.run_sync(
            lambda c: sa_inspect(c).has_table("assets")
            and "owner_group_id" in [col["name"] for col in sa_inspect(c).get_columns("assets")]
        )
        if has_ogid:
            await conn.execute(text(
                "ALTER TABLE assets DROP COLUMN owner_group_id"
            ))

        # -- Drop group_assets.is_owner (all associations are now equal) --
        has_isowner = await conn.run_sync(
            lambda c: sa_inspect(c).has_table("group_assets")
            and "is_owner" in [col["name"] for col in sa_inspect(c).get_columns("group_assets")]
        )
        if has_isowner:
            await conn.execute(text(
                "ALTER TABLE group_assets DROP COLUMN is_owner"
            ))

        # -- devices.supported_codecs --
        has_codecs = await conn.run_sync(lambda c: _has_column(c, "devices", "supported_codecs"))
        if not has_codecs:
            await conn.execute(text(
                "ALTER TABLE devices ADD COLUMN supported_codecs VARCHAR(100) DEFAULT ''"
            ))

        # -- api_keys.key_type --
        has_key_type = await conn.run_sync(lambda c: _has_column(c, "api_keys", "key_type"))
        if not has_key_type:
            await conn.execute(text(
                "ALTER TABLE api_keys ADD COLUMN key_type VARCHAR(10) DEFAULT 'api' NOT NULL"
            ))

    # Run create_all again in case migrations added models with new relationships
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
