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

def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--run-nightly",
        action="store_true",
        default=False,
        help="Run nightly E2E tests (requires docker + agora-device-simulator sibling repo).",
    )


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
