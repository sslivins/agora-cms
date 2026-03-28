"""Shared test fixtures for Agora CMS tests."""

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


# ── In-memory SQLite async engine ──

@pytest_asyncio.fixture
async def db_engine():
    _patch_array_columns()
    engine = create_async_engine("sqlite+aiosqlite://", echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_engine):
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        yield session


# ── FastAPI test app with overridden deps ──

@pytest_asyncio.fixture
async def app(db_engine, tmp_path):
    """Create a test FastAPI app with SQLite DB and temp storage."""
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

    from cms.main import app as fastapi_app
    fastapi_app.dependency_overrides[get_db] = override_get_db
    fastapi_app.dependency_overrides[get_settings] = override_get_settings

    yield fastapi_app

    fastapi_app.dependency_overrides.clear()


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
