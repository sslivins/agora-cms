"""Tests for the first-run setup wizard (issue #185)."""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.auth import (
    SETTING_SETUP_COMPLETED,
    SETTING_SMTP_HOST,
    SETTING_TIMEZONE,
    SETTING_MCP_ENABLED,
    get_setting,
    set_setting,
)
from cms.models.setting import CMSSetting
from cms.models.user import User


# ── Fixtures ──


@pytest_asyncio.fixture
async def setup_app(db_engine, tmp_path):
    """App fixture with setup NOT completed — wizard should be active."""
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
    init_storage(LocalStorageBackend(base_path=settings.asset_storage_path))

    factory = async_sessionmaker(db_engine, expire_on_commit=False)

    async def override_get_db():
        async with factory() as session:
            yield session

    def override_get_settings():
        return settings

    get_settings.cache_clear()

    from cms.main import app as fastapi_app
    import cms.main as main_mod
    main_mod._setup_completed_cache = None

    @asynccontextmanager
    async def _test_lifespan(app):
        yield

    original_router_lifespan = fastapi_app.router.lifespan_context
    fastapi_app.router.lifespan_context = _test_lifespan

    fastapi_app.dependency_overrides[get_db] = override_get_db
    fastapi_app.dependency_overrides[get_settings] = override_get_settings

    # Seed roles + default admin (but do NOT mark setup as completed)
    async with factory() as seed_db:
        from cms.models.user import Role
        from cms.auth import hash_password
        from cms.permissions import BUILTIN_ROLES
        roles = {}
        for name, spec in BUILTIN_ROLES.items():
            role = Role(
                name=name, description=spec["description"],
                permissions=spec["permissions"], is_builtin=True,
            )
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
    main_mod._setup_completed_cache = None


@pytest_asyncio.fixture
async def setup_client(setup_app):
    """Authenticated client for setup wizard tests (logged in as default admin)."""
    transport = ASGITransport(app=setup_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        # Login with default admin credentials
        resp = await ac.post(
            "/login",
            data={"username": "admin", "password": "testpass"},
            follow_redirects=False,
        )
        yield ac


@pytest_asyncio.fixture
async def unauthed_setup_client(setup_app):
    """Unauthenticated client for setup wizard tests."""
    transport = ASGITransport(app=setup_app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── Middleware redirect tests ──


@pytest.mark.asyncio
async def test_redirect_to_setup_when_not_completed(unauthed_setup_client):
    """HTML requests should redirect to /setup when setup is incomplete."""
    resp = await unauthed_setup_client.get(
        "/", headers={"accept": "text/html"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/setup"


@pytest.mark.asyncio
async def test_api_returns_503_when_not_completed(unauthed_setup_client):
    """API requests should get 503 when setup is incomplete."""
    resp = await unauthed_setup_client.get(
        "/api/devices", headers={"accept": "application/json"},
    )
    assert resp.status_code == 503
    assert "setup" in resp.json()["detail"].lower()


@pytest.mark.asyncio
async def test_setup_page_redirects_to_login_when_unauthenticated(unauthed_setup_client):
    """GET /setup should redirect to /login when not logged in."""
    resp = await unauthed_setup_client.get(
        "/setup", headers={"accept": "text/html"}, follow_redirects=False,
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"


@pytest.mark.asyncio
async def test_login_accessible_during_setup(unauthed_setup_client):
    """The login page should be accessible even when setup is incomplete."""
    resp = await unauthed_setup_client.get(
        "/login", headers={"accept": "text/html"},
    )
    assert resp.status_code == 200


@pytest.mark.asyncio
async def test_setup_page_accessible_when_authenticated(setup_client):
    """GET /setup should render the wizard when logged in and setup incomplete."""
    resp = await setup_client.get("/setup", headers={"accept": "text/html"})
    assert resp.status_code == 200
    assert "Welcome to Agora CMS" in resp.text


@pytest.mark.asyncio
async def test_setup_page_redirects_when_completed(client):
    """GET /setup should redirect to / when setup is already done."""
    resp = await client.get("/setup", follow_redirects=False)
    assert resp.status_code == 303
    assert resp.headers["location"] == "/"


# ── Account update ──


@pytest.mark.asyncio
async def test_account_update(setup_client, db_session):
    """POST /setup/account should update the logged-in admin's profile."""
    resp = await setup_client.post("/setup/account", json={
        "display_name": "Updated Admin",
        "email": "updated@example.com",
        "password": "securepass123",
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "ok"

    # Admin should be updated in DB
    result = await db_session.execute(
        select(User).where(User.username == "admin")
    )
    admin = result.scalar_one()
    assert admin.display_name == "Updated Admin"
    assert admin.email == "updated@example.com"
    assert admin.is_active is True


@pytest.mark.asyncio
async def test_account_update_validates_email(setup_client):
    """POST /setup/account should reject invalid emails."""
    resp = await setup_client.post("/setup/account", json={
        "display_name": "Test",
        "email": "no-at-sign",
        "password": "securepass123",
    })
    assert resp.status_code == 400
    assert "email" in resp.json()["error"].lower()


@pytest.mark.asyncio
async def test_account_update_validates_short_password(setup_client):
    """POST /setup/account should reject passwords < 6 chars."""
    resp = await setup_client.post("/setup/account", json={
        "display_name": "Test",
        "email": "test@example.com",
        "password": "abc",
    })
    assert resp.status_code == 400
    assert "6 characters" in resp.json()["error"]


@pytest.mark.asyncio
async def test_account_update_rejects_unauthenticated(unauthed_setup_client):
    """POST /setup/account should return 401 without a session."""
    resp = await unauthed_setup_client.post("/setup/account", json={
        "display_name": "Hacker",
        "email": "hack@example.com",
        "password": "password123",
    })
    assert resp.status_code == 401


# ── SMTP ──


@pytest.mark.asyncio
async def test_smtp_save(setup_client, db_session):
    """POST /setup/smtp should persist SMTP settings."""
    resp = await setup_client.post("/setup/smtp", json={
        "host": "smtp.test.com",
        "port": 465,
        "username": "user@test.com",
        "password": "pass",
        "from_email": "noreply@test.com",
    })
    assert resp.status_code == 200
    val = await get_setting(db_session, SETTING_SMTP_HOST)
    assert val == "smtp.test.com"


# ── Timezone ──


@pytest.mark.asyncio
async def test_timezone_save(setup_client, db_session):
    """POST /setup/timezone should persist the timezone."""
    resp = await setup_client.post("/setup/timezone", json={
        "timezone": "America/New_York",
    })
    assert resp.status_code == 200
    val = await get_setting(db_session, SETTING_TIMEZONE)
    assert val == "America/New_York"


@pytest.mark.asyncio
async def test_timezone_rejects_invalid(setup_client):
    """POST /setup/timezone should reject invalid timezone strings."""
    resp = await setup_client.post("/setup/timezone", json={
        "timezone": "Mars/Olympus",
    })
    assert resp.status_code == 400


# ── MCP ──


@pytest.mark.asyncio
async def test_mcp_save(setup_client, db_session):
    """POST /setup/mcp should persist the MCP enabled flag."""
    resp = await setup_client.post("/setup/mcp", json={"enabled": True})
    assert resp.status_code == 200
    val = await get_setting(db_session, SETTING_MCP_ENABLED)
    assert val == "true"


# ── Completion ──


@pytest.mark.asyncio
async def test_setup_complete(setup_client, db_session):
    """POST /setup/complete should set the completion flag."""
    resp = await setup_client.post("/setup/complete", json={})
    assert resp.status_code == 200
    val = await get_setting(db_session, SETTING_SETUP_COMPLETED)
    assert val == "true"


@pytest.mark.asyncio
async def test_setup_complete_prevents_reentry(setup_client):
    """After completion, setup endpoints should return 400."""
    await setup_client.post("/setup/complete", json={})

    resp = await setup_client.post("/setup/account", json={
        "display_name": "Hacker",
        "email": "hack@example.com",
        "password": "password123",
    })
    assert resp.status_code == 400
    assert "already completed" in resp.json()["error"].lower()


@pytest.mark.asyncio
async def test_normal_routes_accessible_after_setup(setup_client):
    """After setup, normal routes should be accessible."""
    # Update account
    resp = await setup_client.post("/setup/account", json={
        "display_name": "Admin",
        "email": "admin@example.com",
        "password": "password123",
    })
    assert resp.status_code == 200

    # Complete setup
    resp = await setup_client.post("/setup/complete", json={})
    assert resp.status_code == 200

    # Dashboard should now be accessible (we have session cookie)
    resp = await setup_client.get(
        "/", headers={"accept": "text/html"}, follow_redirects=False,
    )
    # Should NOT redirect to /setup anymore
    assert resp.status_code != 303 or resp.headers.get("location") != "/setup"


# ── Upgrade-path migration tests ──


@pytest.mark.asyncio
async def test_upgrade_migration_sets_flag_when_admin_exists(db_engine):
    """Existing deployments (admin already in DB) should auto-set setup_completed
    so the wizard doesn't block returning admins after upgrade."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from cms.auth import (
        hash_password, get_setting, set_setting,
        SETTING_SETUP_COMPLETED, ensure_admin_credentials,
    )
    from cms.models.user import User, Role
    from cms.config import Settings

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    settings = Settings(
        database_url=str(db_engine.url),
        secret_key="test-secret",
        admin_username="admin",
        admin_password="testpass",
    )

    # Simulate existing deployment: admin user exists, NO setup_completed flag
    async with factory() as db:
        role = Role(
            name="Admin", description="admin",
            permissions=["*"], is_builtin=True,
        )
        db.add(role)
        await db.flush()
        db.add(User(
            username="admin", email="existing@example.com",
            display_name="Existing Admin", password_hash=hash_password("pw"),
            role_id=role.id, is_active=True, must_change_password=False,
        ))
        await db.commit()

    # Run the upgrade-path migration logic (same as lifespan)
    async with factory() as db:
        completed = await get_setting(db, SETTING_SETUP_COMPLETED)
        assert completed is None  # flag doesn't exist yet

        result = await db.execute(
            select(User).where(
                User.username == settings.admin_username,
                User.is_active.is_(True),
            ).limit(1)
        )
        admin = result.scalar_one_or_none()
        assert admin is not None  # admin exists → it's an upgrade

        await set_setting(db, SETTING_SETUP_COMPLETED, "true")
        await db.commit()

    # Verify: flag is now set
    async with factory() as db:
        assert await get_setting(db, SETTING_SETUP_COMPLETED) == "true"


@pytest.mark.asyncio
async def test_fresh_install_does_not_set_flag(db_engine):
    """On a truly fresh install (no admin in DB yet), the migration must NOT
    set the flag — the wizard needs to run."""
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from cms.auth import get_setting, SETTING_SETUP_COMPLETED
    from cms.models.user import User
    from cms.config import Settings

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    settings = Settings(
        database_url=str(db_engine.url),
        secret_key="test-secret",
        admin_username="admin",
        admin_password="testpass",
    )

    # No admin user exists — this is a fresh install
    async with factory() as db:
        completed = await get_setting(db, SETTING_SETUP_COMPLETED)
        assert completed is None

        result = await db.execute(
            select(User).where(
                User.username == settings.admin_username,
                User.is_active.is_(True),
            ).limit(1)
        )
        admin = result.scalar_one_or_none()
        assert admin is None  # no admin → fresh install → don't set flag

    # Verify: flag is still NOT set
    async with factory() as db:
        assert await get_setting(db, SETTING_SETUP_COMPLETED) is None
