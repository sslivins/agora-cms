"""Shared test fixtures for Agora CMS tests."""

import asyncio
import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import JSON, event
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from cms.database import Base


# ── Patch ARRAY columns for SQLite compatibility ──
def _patch_array_columns():
    """Replace PostgreSQL ARRAY columns with JSON for SQLite tests."""
    from cms.models.schedule import Schedule
    col = Schedule.__table__.columns["days_of_week"]
    col.type = JSON()


# ── File-based SQLite async engine ──

@pytest_asyncio.fixture
async def db_engine(tmp_path):
    _patch_array_columns()
    db_path = tmp_path / "test.db"
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{db_path}",
        echo=False,
    )
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()
    if db_path.exists():
        os.unlink(db_path)


@pytest_asyncio.fixture
async def db_session(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


# ── FastAPI test app with overridden deps ──

@pytest_asyncio.fixture
async def app(db_engine, tmp_path):
    """Create a test FastAPI app with SQLite DB and temp storage."""
    from unittest.mock import patch

    from cms.auth import get_settings
    from cms.config import Settings
    from cms.database import get_db

    settings = Settings(
        database_url="sqlite+aiosqlite://",
        secret_key="test-secret",
        admin_username="admin",
        admin_password="testpass",
        asset_storage_path=tmp_path / "assets",
    )
    settings.asset_storage_path.mkdir(parents=True, exist_ok=True)

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    def override_get_settings():
        return settings

    # Clear the lru_cache on get_settings so it doesn't return stale real settings.
    get_settings.cache_clear()

    from cms.main import app as fastapi_app
    fastapi_app.dependency_overrides[get_db] = override_get_db
    fastapi_app.dependency_overrides[get_settings] = override_get_settings

    # Point the DB module at our test SQLite engine and prevent the lifespan
    # from overwriting it with a real PostgreSQL engine via init_db().
    import cms.database as db_mod
    db_mod._engine = db_engine
    db_mod._session_factory = factory

    async def _noop_scheduler():
        pass

    with patch("cms.main.init_db"), patch("cms.main.scheduler_loop", _noop_scheduler):
        yield fastapi_app

    fastapi_app.dependency_overrides.clear()
    db_mod._engine = None
    db_mod._session_factory = None
    get_settings.cache_clear()


@pytest_asyncio.fixture
async def client(app):
    """Authenticated async HTTP client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Login to get session cookie
        resp = await ac.post("/login", data={"username": "admin", "password": "testpass"}, follow_redirects=False)
        yield ac


@pytest_asyncio.fixture
async def unauthed_client(app):
    """Unauthenticated async HTTP client."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac
