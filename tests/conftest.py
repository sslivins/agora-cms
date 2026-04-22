"""Shared test fixtures for Agora CMS tests."""

import os
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from cms.database import Base


# ── pytest-xdist per-worker database isolation ────────────────────────
#
# When running under `pytest -n N`, each worker process runs tests in
# parallel against the same Postgres service. The per-test db_engine
# fixture drops and recreates all tables, so multiple workers sharing a
# single database would clobber each other mid-transaction.
#
# To make workers independent, we give each worker its own Postgres
# database named `<orig_db>_<worker_id>` (e.g. agora_test_gw0). The
# database is created on worker startup and dropped on teardown. The
# controller process (no PYTEST_XDIST_WORKER set, or "master") keeps
# the original database so single-process runs are unchanged.


def _ensure_worker_database(base_url: str, worker_id: str) -> str:
    """Create a per-worker Postgres database and return its URL.

    Uses psycopg2 (sync) for the one-off CREATE DATABASE because
    asyncpg doesn't allow DDL outside of a transaction block.
    """
    import re
    from urllib.parse import urlparse, urlunparse

    parsed = urlparse(base_url.replace("+asyncpg", ""))
    orig_db = parsed.path.lstrip("/")
    worker_db = f"{orig_db}_{worker_id}"

    # Connect to the default "postgres" maintenance DB to run CREATE DATABASE.
    import psycopg2
    from psycopg2.extensions import ISOLATION_LEVEL_AUTOCOMMIT

    admin_dsn = urlunparse(parsed._replace(path="/postgres"))
    conn = psycopg2.connect(admin_dsn)
    conn.set_isolation_level(ISOLATION_LEVEL_AUTOCOMMIT)
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT 1 FROM pg_database WHERE datname = %s", (worker_db,)
            )
            if not cur.fetchone():
                # Identifier is constructed from orig_db (which is trusted, it
                # comes from our own env var) + worker_id (pytest-xdist format
                # "gw<int>"). Still, sanity-check the shape.
                if not re.match(r"^[A-Za-z0-9_]+$", worker_db):
                    raise RuntimeError(f"refusing to create suspicious DB name: {worker_db!r}")
                cur.execute(f'CREATE DATABASE "{worker_db}"')
    finally:
        conn.close()

    worker_url = urlunparse(
        urlparse(base_url)._replace(path=f"/{worker_db}")
    )
    return worker_url


def pytest_configure(config: pytest.Config) -> None:
    """On xdist worker startup, switch AGORA_CMS_DATABASE_URL to a per-worker DB."""
    worker_id = os.environ.get("PYTEST_XDIST_WORKER", "")
    # Controller process has no worker id; also skip if xdist isn't in use or
    # we're not using Postgres (e.g. local SQLite dev run).
    if not worker_id or worker_id == "master":
        return
    base_url = os.environ.get("AGORA_CMS_DATABASE_URL", "")
    if not base_url or "postgresql" not in base_url:
        return
    worker_url = _ensure_worker_database(base_url, worker_id)
    os.environ["AGORA_CMS_DATABASE_URL"] = worker_url


# ── nightly opt-in (registered here so `pytest tests/` doesn't try to
# collect tests/nightly/ — whose modules import playwright at module
# scope — unless the user explicitly opts in). ──

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-nightly",
        action="store_true",
        default=False,
        help="Run nightly E2E tests (requires docker + agora-device-simulator sibling repo).",
    )


def _nightly_opted_in(config: pytest.Config) -> bool:
    return bool(
        config.getoption("--run-nightly")
        or os.environ.get("NIGHTLY", "").lower() in ("1", "true", "yes")
    )


def pytest_ignore_collect(collection_path: Path, config: pytest.Config) -> bool | None:
    """Skip collection of tests/nightly/ entirely unless opted in.

    The nightly modules import playwright, httpx, and sqlalchemy at
    module scope; without this guard, unit-test CI (which doesn't
    install playwright) fails with ImportError during collection, and
    the `--run-nightly` skip marker never gets a chance to apply
    because the error happens before modifyitems runs.
    """
    parts = str(collection_path).replace("\\", "/").split("/")
    if "nightly" in parts and "tests" in parts:
        return not _nightly_opted_in(config)
    return None


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
    from cms.models.device_event import DeviceEvent
    from cms.models.log_request import LogRequest
    col = Schedule.__table__.columns["days_of_week"]
    col.type = JSON()
    col = Role.__table__.columns["permissions"]
    col.type = JSON()
    col = AuditLog.__table__.columns["details"]
    col.type = JSON()
    col = Notification.__table__.columns["details"]
    col.type = JSON()
    col = DeviceEvent.__table__.columns["details"]
    col.type = JSON()
    col = LogRequest.__table__.columns["services"]
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

    # SQLite doesn't enforce foreign keys unless the per-connection
    # PRAGMA is set. Without this, ON DELETE SET NULL / CASCADE rules
    # silently no-op in tests, causing real FK regressions to slip past.
    if "sqlite" in db_url:
        from sqlalchemy import event

        @event.listens_for(engine.sync_engine, "connect")
        def _enable_sqlite_fk(dbapi_conn, _conn_record):  # pragma: no cover
            cursor = dbapi_conn.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
            finally:
                cursor.close()

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

    shared_dir = tmp_path / "shared"
    shared_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings(
        database_url=str(db_engine.url),
        secret_key="test-secret",
        admin_username="admin",
        admin_password="testpass",
        asset_storage_path=tmp_path / "assets",
        service_key_path=str(shared_dir / "mcp-service.key"),
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

    # Mark first-run setup as completed so existing tests are not
    # redirected to the setup wizard.
    async with factory() as seed_db:
        from cms.auth import set_setting, SETTING_SETUP_COMPLETED
        await set_setting(seed_db, SETTING_SETUP_COMPLETED, "true")
        await seed_db.commit()

    # Point the DB module at our test engine so any code that
    # accesses database globals directly (e.g. WebSocket handler) works.
    import cms.database as db_mod
    import shared.database as shared_db_mod
    db_mod._engine = db_engine
    db_mod._session_factory = factory
    shared_db_mod._engine = db_engine
    shared_db_mod._session_factory = factory

    yield fastapi_app

    fastapi_app.dependency_overrides.clear()
    fastapi_app.router.lifespan_context = original_router_lifespan
    db_mod._engine = None
    db_mod._session_factory = None
    shared_db_mod._engine = None
    shared_db_mod._session_factory = None
    get_settings.cache_clear()

    # Reset the setup-wizard in-memory cache between tests.
    import cms.main as _main_mod
    _main_mod._setup_completed_cache = None


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
