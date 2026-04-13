"""Shared test fixtures for Agora CMS tests."""

import os

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cms.database import Base


def _get_test_database_url():
    """Return the test database URL.

    Uses AGORA_CMS_DATABASE_URL if set (CI uses PostgreSQL),
    otherwise falls back to SQLite for local dev convenience.
    """
    url = os.environ.get("AGORA_CMS_DATABASE_URL")
    if url:
        return url
    return None  # caller must provide tmp_path-based SQLite URL


def _needs_sqlite_patches(db_url: str) -> bool:
    return "sqlite" in db_url


def _patch_array_columns():
    """Replace PostgreSQL ARRAY/JSONB columns with JSON for SQLite tests."""
    from sqlalchemy import JSON
    from cms.models.schedule import Schedule
    from cms.models.user import Role
    from cms.models.audit_log import AuditLog
    from cms.models.notification import Notification
    col = Schedule.__table__.columns["days_of_week"]
    col.type = JSON()
    col = Role.__table__.columns["permissions"]
    col.type = JSON()
    col = AuditLog.__table__.columns["details"]
    col.type = JSON()
    col = Notification.__table__.columns["details"]
    col.type = JSON()


# ── Database engine ──

@pytest_asyncio.fixture
async def db_engine(tmp_path):
    pg_url = _get_test_database_url()
    if pg_url:
        db_url = pg_url
    else:
        db_path = tmp_path / "test.db"
        db_url = f"sqlite+aiosqlite:///{db_path}"
        _patch_array_columns()

    engine = create_async_engine(db_url, echo=False, poolclass=NullPool)

    async with engine.begin() as conn:
        # For PostgreSQL: drop and recreate all tables for isolation
        if "postgresql" in db_url:
            await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    if "postgresql" in db_url:
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
    """Create a test FastAPI app with temp storage."""
    from contextlib import asynccontextmanager

    from cms.auth import get_settings
    from cms.config import Settings
    from cms.database import get_db
    from cms.services.storage import LocalStorageBackend, init_storage

    settings = Settings(
        database_url=str(db_engine.url),
        secret_key="test-secret",
        admin_username="admin",
        admin_password="testpass",
        asset_storage_path=tmp_path / "assets",
    )
    settings.asset_storage_path.mkdir(parents=True, exist_ok=True)

    # Initialize storage backend for tests
    init_storage(LocalStorageBackend(base_path=settings.asset_storage_path))

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    def override_get_settings():
        return settings

    # Clear the lru_cache on get_settings so it doesn't return stale real settings.
    get_settings.cache_clear()

    from cms.main import app as fastapi_app

    # Replace the real lifespan (which connects to PostgreSQL and runs the
    # scheduler) with a no-op for tests.  Engine cleanup is handled by
    # the db_engine fixture — disposing here races with the anyio portal
    # shutdown and can deadlock the TestClient teardown thread.
    @asynccontextmanager
    async def _test_lifespan(app):
        yield

    original_router_lifespan = fastapi_app.router.lifespan_context
    fastapi_app.router.lifespan_context = _test_lifespan

    fastapi_app.dependency_overrides[get_db] = override_get_db
    fastapi_app.dependency_overrides[get_settings] = override_get_settings

    # Seed RBAC roles and admin user for tests
    async with factory() as seed_db:
        from cms.models.user import Role, User
        from cms.auth import hash_password
        from cms.permissions import BUILTIN_ROLES
        roles = {}
        for name, spec in BUILTIN_ROLES.items():
            role = Role(name=name, description=spec["description"],
                        permissions=spec["permissions"], is_builtin=True)
            seed_db.add(role)
            roles[name] = role
        await seed_db.flush()
        admin_user = User(
            username=settings.admin_username,
            email=settings.admin_email,
            display_name="Test Admin",
            password_hash=hash_password(settings.admin_password),
            role_id=roles["Admin"].id,
            is_active=True,
            must_change_password=False,
        )
        seed_db.add(admin_user)
        await seed_db.commit()

    # Point the DB module at our test engine so any code that
    # accesses database globals directly (e.g. WebSocket handler) works.
    import cms.database as db_mod
    db_mod._engine = db_engine
    db_mod._session_factory = factory

    yield fastapi_app

    fastapi_app.dependency_overrides.clear()
    fastapi_app.router.lifespan_context = original_router_lifespan
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
