"""Session-scoped docker-compose harness for multi-replica integration tests.

Brings up two CMS replicas (``cms-0`` on :8080, ``cms-1`` on :8081)
against a shared Postgres, waits for both ``/healthz`` endpoints to
return 200, then waits for the scheduler lease to be acquired by
*some* replica (so tests that rely on leader-only loops don't race the
first-tick startup window).

Tear-down runs ``docker compose down -v`` so each run starts from a
clean DB — these tests seed whatever fixtures they need.
"""

from __future__ import annotations

import asyncio
import os
import socket
import subprocess
import time
from pathlib import Path
from typing import AsyncIterator, Iterator

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

COMPOSE_FILE = Path(__file__).parent / "docker-compose.integration.yml"
COMPOSE_FILE_WPS = Path(__file__).parent / "docker-compose.integration.wps.yml"
REPO_ROOT = Path(__file__).resolve().parents[2]

# When ``AGORA_INTEGRATION_TRANSPORT=wps`` is set in the env, we layer
# the WPS overlay on top of the base compose file so the same harness
# spins up local-broker + flips both replicas to WPS transport. The
# ``multireplica-wps-smoke`` CI job sets it; the existing
# ``multireplica-smoke`` job leaves it unset (direct-WS path).
INTEGRATION_TRANSPORT = os.environ.get("AGORA_INTEGRATION_TRANSPORT", "local").strip().lower()

CMS_A_URL = "http://127.0.0.1:8080"
CMS_B_URL = "http://127.0.0.1:8081"
# The test process talks to Postgres on the host-exposed port; the CMS
# containers still use the compose-internal ``db:5432`` hostname.
HOST_DB_URL = "postgresql+asyncpg://agora:agora@127.0.0.1:5434/agora_cms"

# Admin creds pinned in the overlay so tests don't depend on whatever
# the developer has in their local .env.
ADMIN_USERNAME = "admin"
ADMIN_PASSWORD = "integration-testpass"

STARTUP_TIMEOUT_SEC = 180
LEADER_TIMEOUT_SEC = 60


def _compose_cmd(*args: str) -> list[str]:
    files = ["-f", str(COMPOSE_FILE)]
    if INTEGRATION_TRANSPORT == "wps":
        files += ["-f", str(COMPOSE_FILE_WPS)]
    return ["docker", "compose", *files, *args]


def _port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _wait_for_healthz(url: str, deadline: float) -> None:
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            resp = httpx.get(f"{url}/healthz", timeout=2.0)
            if resp.status_code == 200:
                return
            last_err = RuntimeError(f"{url}/healthz -> {resp.status_code}")
        except Exception as e:  # pragma: no cover - defensive
            last_err = e
        time.sleep(2.0)
    raise RuntimeError(f"CMS at {url} never became healthy: {last_err}")


async def _wait_for_scheduler_lease_async(engine: AsyncEngine, deadline: float) -> None:
    """Block until *some* replica holds a live ``scheduler`` lease.

    ``/healthz`` returning 200 only tells us uvicorn is up — it does
    **not** mean the lifespan tasks have run far enough to acquire
    leadership. Tests that rely on the scheduler tick (e.g. schedule
    skip propagation) would race this window without the poll.
    """
    sql = text(
        "SELECT holder_id FROM leader_leases "
        "WHERE loop_name = 'scheduler' AND expires_at > NOW()"
    )
    last_err: Exception | None = None
    while time.monotonic() < deadline:
        try:
            async with engine.connect() as conn:
                row = (await conn.execute(sql)).first()
                if row is not None:
                    return
        except Exception as e:
            last_err = e
        await asyncio.sleep(1.0)
    raise RuntimeError(f"scheduler lease never acquired: {last_err}")


def _wait_for_scheduler_lease_sync(deadline: float) -> None:
    """Session-scoped leader-ready check on its own transient engine.

    We deliberately create and dispose an engine inside a single
    ``asyncio.run`` so the connection never outlives the loop that
    owns it — pytest-asyncio gives each test its own loop, and a
    session-scoped asyncpg engine would otherwise yield
    'attached to a different loop' failures on teardown.
    """
    async def _run() -> None:
        eng = create_async_engine(HOST_DB_URL, pool_pre_ping=True)
        try:
            await _wait_for_scheduler_lease_async(eng, deadline)
        finally:
            await eng.dispose()

    asyncio.run(_run())


@pytest.fixture(scope="session")
def compose_stack() -> Iterator[None]:
    # If a previous run left containers, take them down first so we
    # start from a known clean state (and don't collide on ports).
    subprocess.run(
        _compose_cmd("down", "-v"),
        cwd=REPO_ROOT,
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    # ``--wait`` makes compose block until healthchecks pass, but our
    # healthcheck image (python:3.11-slim) has ``urllib`` so we let it
    # run the check. ``--build`` rebuilds the CMS image if the repo
    # source has changed since last run.
    subprocess.run(
        _compose_cmd("up", "-d", "--build", "--wait"),
        cwd=REPO_ROOT,
        check=True,
    )
    deadline = time.monotonic() + STARTUP_TIMEOUT_SEC
    # Belt-and-braces: compose --wait should already block on the
    # healthchecks, but do an explicit poll in case compose's wait
    # returned early for any reason.
    _wait_for_healthz(CMS_A_URL, deadline)
    _wait_for_healthz(CMS_B_URL, deadline)
    # Postgres is exposed on 5434; make sure the port is actually open
    # before the test process tries to connect.
    pg_deadline = time.monotonic() + 30
    while time.monotonic() < pg_deadline and not _port_open("127.0.0.1", 5434):
        time.sleep(1.0)
    try:
        yield
    finally:
        # Keep logs around on failure — leave `--volumes` so the next
        # run starts clean regardless.
        if not os.environ.get("AGORA_INTEGRATION_KEEP_STACK", "").lower() in (
            "1",
            "true",
            "yes",
        ):
            subprocess.run(
                _compose_cmd("down", "-v"),
                cwd=REPO_ROOT,
                check=False,
            )


@pytest.fixture(scope="session")
def leader_ready(compose_stack: None) -> None:
    """One-shot poll that some replica has claimed the scheduler lease."""
    _wait_for_scheduler_lease_sync(time.monotonic() + LEADER_TIMEOUT_SEC)


@pytest_asyncio.fixture
async def engine(leader_ready: None) -> AsyncIterator[AsyncEngine]:
    """Function-scoped engine — owning loop lives for one test only.

    Session-scoping asyncpg-backed engines across pytest-asyncio's
    per-test event loops causes 'attached to a different loop' errors
    on teardown; recreating the engine per test is cheap in
    comparison to the work each test actually does.
    """
    eng = create_async_engine(HOST_DB_URL, pool_pre_ping=True)
    try:
        yield eng
    finally:
        await eng.dispose()


def _login(base_url: str) -> httpx.Client:
    client = httpx.Client(base_url=base_url, timeout=10.0, follow_redirects=False)
    resp = client.post(
        "/login",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    if resp.status_code not in (200, 302, 303):
        raise RuntimeError(f"login to {base_url} failed: {resp.status_code} {resp.text[:200]}")
    return client


@pytest.fixture
def client_a(leader_ready: None) -> Iterator[httpx.Client]:
    c = _login(CMS_A_URL)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def client_b(leader_ready: None) -> Iterator[httpx.Client]:
    c = _login(CMS_B_URL)
    try:
        yield c
    finally:
        c.close()
