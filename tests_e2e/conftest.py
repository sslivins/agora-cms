"""Shared fixtures for Playwright end-to-end tests.

Starts a real CMS server backed by SQLite (no Docker/PostgreSQL needed)
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
    """Start a real CMS server with SQLite in a background thread.

    Environment variables MUST be set before importing any cms module so that
    Settings picks up SQLite and the test paths.
    """
    tmp = tmp_path_factory.mktemp("e2e")
    db_path = tmp / "e2e.db"
    asset_path = tmp / "assets"
    asset_path.mkdir()

    os.environ["AGORA_CMS_DATABASE_URL"] = f"sqlite+aiosqlite:///{db_path}"
    os.environ["AGORA_CMS_SECRET_KEY"] = "e2e-test-secret"
    os.environ["AGORA_CMS_ADMIN_USERNAME"] = "admin"
    os.environ["AGORA_CMS_ADMIN_PASSWORD"] = "testpass"
    os.environ["AGORA_CMS_ASSET_STORAGE_PATH"] = str(asset_path)
    os.environ["AGORA_CMS_API_KEY_ROTATION_HOURS"] = "0"

    # Patch PostgreSQL ARRAY → JSON for SQLite before any model import
    from sqlalchemy import JSON as SA_JSON
    from cms.models.schedule import Schedule
    col = Schedule.__table__.columns["days_of_week"]
    col.type = SA_JSON()

    from cms.models.user import Role
    col = Role.__table__.columns["permissions"]
    col.type = SA_JSON()

    # Clear cached settings so they pick up the new env vars
    from cms.auth import get_settings
    get_settings.cache_clear()

    # Monkey-patch run_migrations to skip PostgreSQL-specific ALTER TYPE commands
    import cms.database as db_mod
    _orig_run_migrations = db_mod.run_migrations

    async def _sqlite_safe_migrations():
        """Only run create_all (skip ALTER TYPE for pg_enum)."""
        async with db_mod._engine.begin() as conn:
            await conn.run_sync(db_mod.Base.metadata.create_all)

    db_mod.run_migrations = _sqlite_safe_migrations

    from cms.main import app

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=e2e_port,
        log_level="warning",
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

    yield server

    server.should_exit = True
    thread.join(timeout=5)

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
    )
    page = ctx.new_page()
    page.goto("/login")
    page.fill('input[name="username"]', "admin")
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
