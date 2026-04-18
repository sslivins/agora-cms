"""Shared fixtures for Playwright end-to-end tests.

Starts a real CMS server backed by PostgreSQL (in CI) or SQLite (local dev)
on a random free port, then provides a Playwright browser context pre-
authenticated via session cookie.
"""

import asyncio
import os
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

# ── Helpers ──


def _free_port() -> int:
    """Find a free TCP port on localhost."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_async(coro):
    """Run an async coroutine from sync test code.

    Uses a new thread to avoid 'cannot call from running event loop' errors
    when pytest-asyncio/anyio already owns the main-thread loop.
    """
    result = [None]
    error = [None]

    def _target():
        try:
            result[0] = asyncio.run(coro)
        except Exception as e:
            error[0] = e

    t = threading.Thread(target=_target)
    t.start()
    t.join(timeout=30)
    if t.is_alive():
        raise TimeoutError("run_async() timed out after 30s")
    if error[0]:
        raise error[0]
    return result[0]


# ── Fixtures ──


@pytest.fixture(scope="session")
def e2e_port():
    return _free_port()


@pytest.fixture(scope="session")
def base_url(e2e_port):
    return f"http://127.0.0.1:{e2e_port}"


@pytest.fixture(scope="session")
def ws_url(e2e_port):
    return f"ws://127.0.0.1:{e2e_port}/ws/device"


@pytest.fixture(scope="session")
def e2e_server(e2e_port, tmp_path_factory):
    """Start a real CMS server in a background thread.

    Uses PostgreSQL when AGORA_CMS_DATABASE_URL is set (CI), otherwise
    falls back to SQLite for local dev convenience.
    """
    tmp = tmp_path_factory.mktemp("e2e")
    asset_path = tmp / "assets"
    asset_path.mkdir()

    pg_url = os.environ.get("AGORA_CMS_DATABASE_URL")
    using_sqlite = not pg_url

    if using_sqlite:
        db_path = tmp / "e2e.db"
        os.environ["AGORA_CMS_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    # else: AGORA_CMS_DATABASE_URL is already set to PostgreSQL

    os.environ["AGORA_CMS_SECRET_KEY"] = "e2e-test-secret"
    os.environ["AGORA_CMS_ADMIN_USERNAME"] = "admin"
    os.environ["AGORA_CMS_ADMIN_PASSWORD"] = "testpass"
    os.environ["AGORA_CMS_ASSET_STORAGE_PATH"] = str(asset_path)
    os.environ["AGORA_CMS_API_KEY_ROTATION_HOURS"] = "0"

    if using_sqlite:
        # Patch PostgreSQL ARRAY/JSONB → JSON for SQLite before any model import
        from sqlalchemy import JSON as SA_JSON
        from cms.models.schedule import Schedule
        col = Schedule.__table__.columns["days_of_week"]
        col.type = SA_JSON()

        from cms.models.user import Role
        col = Role.__table__.columns["permissions"]
        col.type = SA_JSON()

        from cms.models.audit_log import AuditLog
        col = AuditLog.__table__.columns["details"]
        col.type = SA_JSON()

        from cms.models.notification import Notification
        col = Notification.__table__.columns["details"]
        col.type = SA_JSON()

        from cms.models.device_event import DeviceEvent
        col = DeviceEvent.__table__.columns["details"]
        col.type = SA_JSON()

    # Clear cached settings so they pick up the new env vars
    from cms.auth import get_settings
    get_settings.cache_clear()

    if using_sqlite:
        # Bypass Alembic for SQLite e2e runs.  Alembic migrations use
        # PostgreSQL-specific types (pg_enum, JSONB, etc.) that SQLite
        # can't execute.  Base.metadata.create_all uses the model types
        # with whatever dialect-specific overrides this conftest applied
        # above, which is exactly what these tests want.
        import cms.database as db_mod
        from shared import database as _shared_db
        _orig_run_migrations = db_mod.run_migrations

        async def _sqlite_safe_migrations():
            async with _shared_db._engine.begin() as conn:
                await conn.run_sync(db_mod.Base.metadata.create_all)

        db_mod.run_migrations = _sqlite_safe_migrations
    else:
        import cms.database as db_mod
        _orig_run_migrations = None

    from cms.main import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=e2e_port,
        log_level="info" if os.environ.get("CI") else "warning",
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to be ready
    import httpx
    for _ in range(200):
        try:
            r = httpx.get(f"http://127.0.0.1:{e2e_port}/login", timeout=2.0)
            if r.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout):
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError("E2E server failed to start")

    # Mark first-run setup as completed so existing tests aren't
    # blocked by the setup wizard middleware.
    # Walk through the setup wizard via HTTP to set the flag properly,
    # avoiding async event-loop cross-thread issues with direct DB access.
    # Retry the whole sequence if the server drops the connection (CI flake).
    base = f"http://127.0.0.1:{e2e_port}"

    def _complete_setup_wizard():
        with httpx.Client(base_url=base, follow_redirects=False, timeout=10.0) as c:
            c.post("/login", data={
                "username": os.environ.get("AGORA_CMS_ADMIN_USERNAME", "admin"),
                "password": os.environ["AGORA_CMS_ADMIN_PASSWORD"],
            })
            c.post("/setup/account", data={
                "display_name": "Admin",
                "email": "admin@localhost",
                "password": "",
                "password_confirm": "",
            })
            c.post("/setup/smtp", data={})
            c.post("/setup/timezone", data={"timezone": "UTC"})
            c.post("/setup/mcp", json={"enabled": False})
            c.post("/setup/complete")

    last_exc = None
    for attempt in range(3):
        try:
            _complete_setup_wizard()
            break
        except (httpx.RemoteProtocolError, httpx.ConnectError, httpx.ReadError) as exc:
            last_exc = exc
            time.sleep(2)
    else:
        raise RuntimeError(
            f"Setup wizard failed after 3 attempts: {last_exc}"
        ) from last_exc

    # Post-setup health check — confirm server is still responsive
    for _ in range(50):
        try:
            r = httpx.get(f"{base}/healthz", timeout=2.0)
            if r.status_code == 200:
                break
        except (httpx.ConnectError, httpx.ReadError, httpx.ConnectTimeout):
            pass
        time.sleep(0.1)
    else:
        raise RuntimeError("Server not responding after setup wizard completed")

    yield server

    server.should_exit = True
    thread.join(timeout=5)

    if _orig_run_migrations:
        db_mod.run_migrations = _orig_run_migrations

    for key in list(os.environ):
        if key.startswith("AGORA_CMS_"):
            del os.environ[key]
    get_settings.cache_clear()


@pytest.fixture(scope="session")
def browser_instance():
    """Launch a single browser for the test session."""
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def context(browser_instance: Browser, base_url: str, e2e_server) -> BrowserContext:
    """Create a fresh browser context and log in."""
    ctx = browser_instance.new_context(
        base_url=base_url,
        ignore_https_errors=True,
        viewport={"width": 1280, "height": 1024},
    )
    page = ctx.new_page()
    page.goto("/login")
    page.fill('input[name="email"]', "admin")
    page.fill('input[name="password"]', "testpass")
    page.click('button[type="submit"]')
    page.wait_for_url("**/")
    page.close()
    yield ctx
    ctx.close()


@pytest.fixture
def page(context: BrowserContext) -> Page:
    """Provide a logged-in page."""
    pg = context.new_page()
    yield pg
    pg.close()


@pytest.fixture
def api(context: BrowserContext, base_url: str):
    """Provide a helper for making authenticated API calls."""
    import httpx

    cookies = {c["name"]: c["value"] for c in context.cookies()}

    class ApiHelper:
        def __init__(self):
            self._client = httpx.Client(
                base_url=base_url,
                cookies=cookies,
                timeout=10.0,
            )

        def post(self, path, **kwargs):
            return self._client.post(path, **kwargs)

        def get(self, path, **kwargs):
            return self._client.get(path, **kwargs)

        def patch(self, path, **kwargs):
            return self._client.patch(path, **kwargs)

        def delete(self, path, **kwargs):
            return self._client.delete(path, **kwargs)

        def create_asset(self, filename="test-video.mp4", content=b"fake-mp4-content"):
            """Upload a fake asset file."""
            import io
            return self._client.post(
                "/api/assets/upload",
                files={"file": (filename, io.BytesIO(content), "video/mp4")},
            )

    helper = ApiHelper()
    yield helper
    helper._client.close()


@pytest.fixture
def setup_incomplete(e2e_server):
    """Temporarily clear setup_completed so the setup wizard is active.

    Restores the flag after the test so other tests are not affected.
    Creates a fresh async engine to avoid event-loop cross-thread issues
    with the server's engine running in uvicorn's thread.
    """
    import cms.main as main_mod

    def _run_sql(sql_text, params=None):
        """Run SQL via a temporary async engine in a fresh event loop."""
        from sqlalchemy.ext.asyncio import create_async_engine
        from sqlalchemy import text as sa_text

        db_url = os.environ["AGORA_CMS_DATABASE_URL"]

        async def _exec():
            engine = create_async_engine(db_url)
            async with engine.begin() as conn:
                await conn.execute(sa_text(sql_text), params or {})
            await engine.dispose()

        run_async(_exec())

    # Clear the flag
    _run_sql("DELETE FROM cms_settings WHERE key = 'setup_completed'")
    main_mod._setup_completed_cache = None

    yield

    # Restore the flag
    _run_sql(
        "INSERT INTO cms_settings (key, value) VALUES ('setup_completed', 'true') "
        "ON CONFLICT (key) DO UPDATE SET value = 'true'"
    )
    main_mod._setup_completed_cache = True
