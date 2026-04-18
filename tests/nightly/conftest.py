"""Session-level pytest fixtures for the nightly E2E suite.

Brings up a full `docker compose` stack (base + nightly overlay) at the
start of the test session, yields a `StackHandle` with service URLs, and
tears the stack down (with `down -v`) at the end.

Enable the nightly suite with `--run-nightly` or `NIGHTLY=1 pytest tests/nightly`.
Without that opt-in the tests are skipped so regular `pytest` doesn't try to
stand up docker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import httpx
import pytest

from tests.nightly.helpers import MailpitClient, SimulatorClient

REPO_ROOT = Path(__file__).resolve().parents[2]
NIGHTLY_DIR = REPO_ROOT / "tests" / "nightly"
BASE_COMPOSE = REPO_ROOT / "docker-compose.yml"
OVERLAY_COMPOSE = NIGHTLY_DIR / "docker-compose.nightly.yml"
ENV_FIXTURE = NIGHTLY_DIR / ".env.nightly"
SIMULATOR_REPO = REPO_ROOT.parent / "agora-device-simulator"

STARTUP_TIMEOUT = float(os.environ.get("NIGHTLY_STARTUP_TIMEOUT", "300"))
PROJECT_NAME = os.environ.get("NIGHTLY_PROJECT", "agora-nightly")


# ── opt-in flag ──
#
# `--run-nightly` is registered in `tests/conftest.py` (one level up)
# so that `pytest tests/` skips the entire nightly dir at collection
# time — otherwise these modules' module-scope `from playwright.sync_api
# import ...` statements crash the unit-test CI where playwright isn't
# installed. We reuse the same flag here for the skip-vs-timeout
# marker logic below.


def _nightly_enabled(config: pytest.Config) -> bool:
    return bool(
        config.getoption("--run-nightly")
        or os.environ.get("NIGHTLY", "").lower() in ("1", "true", "yes")
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if _nightly_enabled(config):
        # Nightly tests need a generous timeout: the session fixture does
        # `docker compose up -d --build` (can take minutes on a cold cache).
        nightly_timeout = pytest.mark.timeout(
            int(os.environ.get("NIGHTLY_TEST_TIMEOUT", "900"))
        )
        for item in items:
            if "tests/nightly" in str(item.fspath).replace("\\", "/"):
                item.add_marker(nightly_timeout)
        return
    skip = pytest.mark.skip(
        reason="nightly tests are opt-in; pass --run-nightly or set NIGHTLY=1"
    )
    for item in items:
        if "tests/nightly" in str(item.fspath).replace("\\", "/"):
            item.add_marker(skip)


# ── stack lifecycle ──

@dataclass
class StackHandle:
    cms_url: str
    mailpit_url: str
    simulator_url: str
    postgres_dsn: str

    @property
    def mailpit(self) -> MailpitClient:
        return MailpitClient(self.mailpit_url)

    @property
    def simulator(self) -> SimulatorClient:
        return SimulatorClient(self.simulator_url)


def _compose(*args: str, check: bool = True, capture: bool = False) -> subprocess.CompletedProcess:
    cmd = [
        "docker", "compose",
        "-p", PROJECT_NAME,
        "-f", str(BASE_COMPOSE),
        "-f", str(OVERLAY_COMPOSE),
        *args,
    ]
    # Compose resolves build contexts relative to CWD, so run from REPO_ROOT.
    return subprocess.run(
        cmd, cwd=REPO_ROOT, check=check,
        capture_output=capture, text=True,
    )


def _require_preconditions() -> None:
    if shutil.which("docker") is None:
        pytest.skip("docker is not installed — nightly tests require docker")
    if not SIMULATOR_REPO.is_dir():
        pytest.skip(
            f"{SIMULATOR_REPO} not found. Clone agora-device-simulator as a "
            "sibling of agora-cms so the simulator image can be built."
        )
    sim_submod = SIMULATOR_REPO / "agora" / "cms_client"
    if not sim_submod.is_dir():
        pytest.skip(
            f"{sim_submod} missing — run `git submodule update --init` "
            "in the agora-device-simulator checkout."
        )


def _ensure_env_file() -> Path:
    """Write .env alongside docker-compose.yml if one doesn't already exist.

    The base compose references `env_file: .env`. In CI we ship our own
    nightly values. If the user has their own .env we leave it untouched
    (their postgres password may match other state).
    """
    target = REPO_ROOT / ".env"
    if target.exists():
        return target
    target.write_bytes(ENV_FIXTURE.read_bytes())
    return target


def _wait_for_service(name: str, probe, timeout: float) -> None:
    """Poll `probe()` until it returns truthy, or fail the session with logs."""
    deadline = time.monotonic() + timeout
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            if probe():
                return
        except Exception as exc:  # noqa: BLE001
            last_err = exc
        time.sleep(1.0)
    logs = _compose("logs", "--tail", "200", name, check=False, capture=True)
    pytest.fail(
        f"Service '{name}' did not become ready within {timeout}s "
        f"(last error: {last_err}).\n--- logs ---\n{logs.stdout}\n{logs.stderr}"
    )


@pytest.fixture(scope="session")
def stack(request: pytest.FixtureRequest) -> Iterator[StackHandle]:
    if not _nightly_enabled(request.config):
        pytest.skip("nightly suite not enabled")
    _require_preconditions()
    _ensure_env_file()

    # Clean slate: tear down any previous project with the same name.
    _compose("down", "-v", "--remove-orphans", check=False, capture=True)

    try:
        _compose("up", "-d", "--build")

        handle = StackHandle(
            cms_url=os.environ.get("NIGHTLY_CMS_URL", "http://127.0.0.1:8080"),
            mailpit_url=os.environ.get("NIGHTLY_MAILPIT_URL", "http://127.0.0.1:8025"),
            simulator_url=os.environ.get("NIGHTLY_SIMULATOR_URL", "http://127.0.0.1:9090"),
            postgres_dsn=os.environ.get(
                "NIGHTLY_POSTGRES_DSN",
                "postgresql://agora:agora@127.0.0.1:5433/agora_cms",
            ),
        )

        _wait_for_service(
            "cms",
            lambda: httpx.get(f"{handle.cms_url}/healthz", timeout=2.0).status_code == 200,
            STARTUP_TIMEOUT,
        )
        _wait_for_service(
            "mailpit",
            lambda: MailpitClient(handle.mailpit_url).is_ready(),
            STARTUP_TIMEOUT,
        )
        _wait_for_service(
            "simulator",
            lambda: SimulatorClient(handle.simulator_url).is_ready(),
            STARTUP_TIMEOUT,
        )

        yield handle
    finally:
        tear_down = os.environ.get("NIGHTLY_KEEP_STACK", "").lower() not in ("1", "true", "yes")
        if tear_down:
            # Capture logs first — useful on failure, harmless on success.
            logs_path = NIGHTLY_DIR / "last-run-logs.txt"
            try:
                completed = _compose("logs", "--no-color", "--tail", "1000",
                                     check=False, capture=True)
                logs_path.write_text(
                    (completed.stdout or "") + "\n" + (completed.stderr or ""),
                    encoding="utf-8", errors="replace",
                )
            except Exception as exc:  # noqa: BLE001
                print(f"[nightly] warning: failed to capture compose logs: {exc}",
                      file=sys.stderr)
            _compose("down", "-v", "--remove-orphans", check=False, capture=True)
        else:
            print("[nightly] NIGHTLY_KEEP_STACK set — leaving stack running",
                  file=sys.stderr)


# ── per-test client fixtures ──

@pytest.fixture
def mailpit(stack: StackHandle) -> Iterator[MailpitClient]:
    with stack.mailpit as client:
        # Clear mailbox between tests so assertions on "the latest email" are
        # deterministic.
        client.delete_all()
        yield client


@pytest.fixture
def simulator(stack: StackHandle) -> Iterator[SimulatorClient]:
    with stack.simulator as client:
        yield client


@pytest.fixture
def cms_base_url(stack: StackHandle) -> str:
    return stack.cms_url


@pytest.fixture
def admin_credentials() -> tuple[str, str]:
    """Bootstrap admin credentials baked into the nightly compose overlay."""
    return ("admin", "nightly-testpass")


# Post-OOBE admin creds — test_01_oobe.py personalizes the admin account to
# these values during the wizard walk, and every subsequent phase uses them
# to log in. Kept here (not in test_01_oobe) so other phase test modules can
# depend on them without importing from a test module.
POST_OOBE_ADMIN_NAME = "Nightly Admin"
POST_OOBE_ADMIN_EMAIL = "nightly-admin@agora.test"
POST_OOBE_ADMIN_PASSWORD = "nightly-newpass-123"


@pytest.fixture
def post_oobe_admin() -> tuple[str, str]:
    """(email, password) of the admin account after the OOBE wizard has run."""
    return (POST_OOBE_ADMIN_EMAIL, POST_OOBE_ADMIN_PASSWORD)


# ── Playwright fixtures ──
#
# Imported lazily so users running just the sanity tests without playwright
# installed still get a useful skip rather than a collection error.

@pytest.fixture(scope="session")
def playwright_browser():
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        pytest.skip("playwright not installed — `pip install playwright && playwright install chromium`")
    headless = os.environ.get("NIGHTLY_HEADLESS", "1").lower() not in ("0", "false", "no")
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=headless)
        try:
            yield browser
        finally:
            browser.close()


@pytest.fixture
def browser_context(playwright_browser, cms_base_url: str):
    ctx = playwright_browser.new_context(base_url=cms_base_url, ignore_https_errors=True)
    try:
        yield ctx
    finally:
        ctx.close()


@pytest.fixture
def page(browser_context):
    pg = browser_context.new_page()
    try:
        yield pg
    finally:
        pg.close()


@pytest.fixture
def authenticated_page(browser_context, post_oobe_admin: tuple[str, str]):
    """A Page already logged in as the post-OOBE admin.

    Depends on `test_01_oobe.py` having walked the wizard earlier in the
    session. Later phases import this fixture instead of re-walking the
    wizard.

    Playwright's `page.request` inherits cookies from the browser context,
    so tests using this fixture can also drive `/api/*` endpoints with the
    authenticated session.
    """
    import re
    pg = browser_context.new_page()
    email, password = post_oobe_admin
    try:
        pg.goto("/login")
        pg.fill('input[name="email"]', email)
        pg.fill('input[name="password"]', password)
        pg.click('button[type="submit"]')
        pg.wait_for_url(re.compile(r".*/(?!login).*$|.*/$|.*/dashboard.*"), timeout=15_000)
        assert "/login" not in pg.url, f"login with post-OOBE creds failed, still on {pg.url}"
        yield pg
    finally:
        pg.close()
