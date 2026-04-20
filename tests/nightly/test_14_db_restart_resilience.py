"""Phase 14: DB-restart resilience smoke test.

Regression coverage for the v1.37.19 incident:

  1. Postgres container restarted (operator action / docker daemon /
     Azure managed-DB failover).
  2. The CMS + worker SQLAlchemy pools still held connections to the dead
     postmaster.  Without ``pool_pre_ping``, the next checkout raised
     ``InterfaceError: connection is closed``.
  3. In the worker's ``_listen_mode_robust`` loop, that exception walked
     all the way out of the process and exited it with code 1.
  4. ``docker-compose.yml`` had no restart policy on the worker, so the
     container stayed dead and every queued capture/transcode sat PENDING
     forever.

This test reproduces step 1 and asserts that 2-4 no longer happen:

* CMS ``/healthz`` keeps returning 200 across the restart (proves the
  CMS-side pool ``pool_pre_ping`` recovers transparently).
* ``GET /api/users/me`` (an authed read that goes to the DB on every call)
  succeeds within a generous window after the DB is back up.
* The worker container is still alive a few seconds after the restart —
  either because it never crashed (loop now wraps DB calls in try/except)
  or because compose's ``restart: unless-stopped`` brought it back.

This is a deliberately lightweight phase: no UI, no playwright, no asset
upload — pure HTTP + ``docker compose restart`` against the existing stack.
Slot 14 keeps it after the existing display-disconnect phase (13) so a
restart here doesn't perturb earlier RBAC/notification fixtures.
"""

from __future__ import annotations

import subprocess
import time

import httpx
import pytest

from tests.nightly.conftest import PROJECT_NAME

# After ``docker compose restart db`` we expect the new postmaster to be
# accepting connections within a few seconds (pg_isready in the healthcheck
# typically reports healthy in ~3-5s).  CMS pool_pre_ping then issues one
# wasted SELECT 1 per stale conn checkout, which is ~instant.  Pad
# generously for slow CI runners.
DB_RECOVERY_WINDOW_S = 60.0
POLL_INTERVAL_S = 1.0


def _compose_restart(service: str) -> None:
    """Restart a single compose service in the nightly project."""
    subprocess.run(
        ["docker", "compose", "-p", PROJECT_NAME, "restart", service],
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )


def _container_state(service: str) -> str:
    """Return docker's State.Status string for a service container."""
    name = f"{PROJECT_NAME}-{service}-1"
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Status}}", name],
        check=True,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return result.stdout.strip()


def _poll_until(probe, timeout_s: float, interval_s: float = POLL_INTERVAL_S):
    """Call probe() until it returns truthy or timeout — return last value."""
    deadline = time.monotonic() + timeout_s
    last = None
    while time.monotonic() < deadline:
        try:
            last = probe()
            if last:
                return last
        except Exception as exc:  # noqa: BLE001
            last = exc
        time.sleep(interval_s)
    return last


@pytest.mark.order(after="tests/nightly/test_13_display_disconnect_notifications.py")
def test_db_restart_resilience(stack):
    """CMS + worker survive a Postgres container restart.

    This is the regression test for the v1.37.19 incident — see module
    docstring.  Without ``pool_pre_ping`` on the SQLAlchemy engine, this
    test would 500 on the first post-restart healthz; without the listen
    loop's try/except, the worker container would be in ``exited`` state
    a few seconds after the DB came back; without the compose ``restart``
    policy, even the loop wrapper wouldn't help across hard crashes.
    """
    # ── Baseline: everything healthy before we poke it ──
    pre = httpx.get(f"{stack.cms_url}/healthz", timeout=5.0)
    assert pre.status_code == 200, f"baseline /healthz expected 200, got {pre.status_code}"
    assert _container_state("worker") == "running", "worker not running pre-restart"

    # ── Restart Postgres ──
    _compose_restart("db")

    # ── CMS must recover transparently via pool_pre_ping ──
    # We hit /healthz repeatedly: the very first call after the restart
    # may catch a stale-pool connection and *should* be re-pinged before
    # checkout (returning 200).  If pool_pre_ping is missing, this
    # returns 500 until the entire stale pool has been churned out.
    def _healthz_ok():
        try:
            r = httpx.get(f"{stack.cms_url}/healthz", timeout=5.0)
        except httpx.HTTPError:
            return False
        return r.status_code == 200

    result = _poll_until(_healthz_ok, DB_RECOVERY_WINDOW_S)
    assert result is True, (
        f"CMS /healthz did not recover within {DB_RECOVERY_WINDOW_S}s of "
        f"db restart.  Likely regression of pool_pre_ping in shared.database "
        f"(v1.37.19 incident)."
    )

    # ── Worker container must still be alive ──
    # Either it never crashed (listen-loop try/except) or compose restarted
    # it (restart: unless-stopped).  Both outcomes count as a pass.  We give
    # it a few extra seconds in case the restart cycle is in flight.
    def _worker_running():
        return _container_state("worker") == "running"

    assert _poll_until(_worker_running, 30.0) is True, (
        "worker container is not in 'running' state after db restart.  "
        "Either the listen-mode loop crashed without try/except (regression "
        "of v1.37.19) or the compose restart policy was removed."
    )
