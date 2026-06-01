"""Phase 9d: Logs round-trip — ``POST /api/logs/requests`` (#345).

Covers the end-to-end path for the "Get Logs" UI action, now via the
multi-replica-safe async outbox introduced in PR #345:

1. Happy-ish path — ``POST /api/logs/requests`` enqueues an outbox row,
   CMS forwards the ``request_logs`` command over WS, and the simulator
   records it with ``services`` and ``since`` as sent. The fixture
   installs a small ``logs_synth`` profile on the device so the sim's
   journalctl shim returns synthetic bytes promptly (instead of
   blocking on a real ``journalctl`` subprocess that hangs without
   dbus inside the smoke container — see notes on the prior flake of
   this test). The device replies with ``logs_response`` which flips
   the row to ``ready``; we poll ``GET /api/logs/requests/{id}`` until
   the row reaches a terminal state (ready or failed). Proof of
   delivery is the recorded command on the sim, which is independent
   of the terminal row status.

2. Unknown device — ``POST /api/logs/requests`` returns 404 from the
   access-check guard that loads the device row.

3. Disconnected device — forcing the target offline first still returns
   202 (row created + queued); the terminal status will be "pending"
   until the device reconnects and replies, so we just assert the row
   exists and stays in a non-ready state.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.nightly.helpers.simulator import SimulatorClient


LOGS_RESPONSE_TIMEOUT_S = 45.0
LOGS_POLL_TIMEOUT_S = 45.0
OFFLINE_DURATION_S = 20.0
OFFLINE_DETECT_TIMEOUT_S = 30.0
ONLINE_DETECT_TIMEOUT_S = 45.0

# Small per-service synthetic payload installed on the sim so that
# ``_handle_request_logs`` short-circuits the real journalctl
# subprocess (which can block ~30s per service in the smoke container
# where dbus is unavailable). 5 KB × 2 services keeps us comfortably
# under the WS JSON-path frame ceiling.
SYNTH_BYTES_PER_SERVICE = 5 * 1024


# ── helpers ──────────────────────────────────────────────────────────────


def _api_get(page: Page, path: str) -> Any:
    resp = page.request.get(path)
    assert resp.status == 200, f"GET {path} -> {resp.status}: {resp.text()[:400]}"
    return resp.json()


def _adopted_device_ids(page: Page) -> list[str]:
    return sorted(
        d["id"] for d in _api_get(page, "/api/devices")
        if d.get("status") == "adopted"
    )


def _wait_for_online(page: Page, device_id: str, expected: bool, *, timeout: float):
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _api_get(page, f"/api/devices/{device_id}")
        if bool(last.get("is_online")) == expected:
            return last
        time.sleep(0.5)
    raise AssertionError(
        f"device {device_id} is_online != {expected} after {timeout}s: {last!r}"
    )


def _poll_log_request(
    page: Page, request_id: str, *, timeout: float,
    terminal: tuple[str, ...] = ("ready", "failed"),
) -> dict:
    """Poll ``GET /api/logs/requests/{id}`` until status is terminal or timeout."""
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = page.request.get(f"/api/logs/requests/{request_id}")
        assert resp.status == 200, (
            f"GET /api/logs/requests/{request_id} -> {resp.status}: {resp.text()[:400]}"
        )
        last = resp.json()
        if last.get("status") in terminal:
            return last
        time.sleep(1.0)
    return last


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def logs_device(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> str:
    simulator.wait_for_devices(expected_count=3, timeout=60.0)
    ids = _adopted_device_ids(authenticated_page)
    assert ids, "no adopted devices — test_03_devices must have run first"
    # Use the FIRST adopted device — test_11 already exercises the last one
    # for lifecycle, and this gives the two phases cleanly-partitioned
    # targets so their recording buffers don't interfere.
    dev_id = ids[0]
    _wait_for_online(authenticated_page, dev_id, True, timeout=30.0)
    simulator.reset_recording(dev_id)
    # Install a small synthetic-logs profile so the sim's
    # ``_handle_request_logs`` skips the real journalctl subprocess
    # (which can block ~30s per service inside the smoke container).
    # Without this, the row can stay at ``sent`` past LOGS_POLL_TIMEOUT_S
    # and the test flakes.
    simulator.set_logs(
        dev_id,
        {svc: SYNTH_BYTES_PER_SERVICE for svc in
         ("agora-player", "agora-cms-client")},
    )
    try:
        yield dev_id
    finally:
        # Always clear so downstream tests see vanilla sim behaviour.
        try:
            simulator.clear_logs(dev_id)
        except Exception:
            pass


# ── tests ────────────────────────────────────────────────────────────────


def test_request_logs_delivers_request_logs_command_to_device(
    authenticated_page: Page,
    simulator: SimulatorClient,
    logs_device: str,
) -> None:
    """POST /api/logs/requests -> the simulator records a ``request_logs``
    command containing request_id, services, since; the outbox row flips
    to a terminal status (ready or failed)."""
    page = authenticated_page
    device_id = logs_device

    services = ["agora-player", "agora-cms-client"]
    since = "1h"
    resp = page.request.post(
        "/api/logs/requests",
        data={"device_id": device_id, "services": services, "since": since},
        timeout=15_000,
    )
    assert resp.status == 202, (
        f"POST /api/logs/requests -> {resp.status}: {resp.text()[:400]}"
    )
    created = resp.json()
    request_id = created.get("request_id")
    assert isinstance(request_id, str) and request_id, (
        f"expected request_id in response, got: {created!r}"
    )
    # When the device is online we expect the initial dispatch to succeed
    # synchronously and the API to report "sent".
    assert created.get("status") == "sent", (
        f"expected status=sent for online device, got: {created!r}"
    )

    # The simulator must have recorded the command — this is the real
    # proof-of-delivery irrespective of the eventual row status.
    matches = simulator.wait_for_command(
        device_id, "request_logs", timeout=LOGS_RESPONSE_TIMEOUT_S,
    )
    assert matches, "wait_for_command returned no matches"
    payload = matches[-1].get("payload") or {}
    assert payload.get("type") == "request_logs"
    assert payload.get("services") == services
    assert payload.get("since") == since
    assert payload.get("request_id") == request_id

    # The row should eventually reach a terminal state. The fixture
    # installs a small logs_synth profile so the sim returns synthetic
    # logs promptly and the row goes "ready"; we still accept "failed"
    # to keep this test resilient to changes in the sim's error path.
    final = _poll_log_request(page, request_id, timeout=LOGS_POLL_TIMEOUT_S)
    assert final.get("status") in ("ready", "failed"), (
        f"log request {request_id} did not reach terminal status in "
        f"{LOGS_POLL_TIMEOUT_S}s: {final!r}"
    )


def test_request_logs_unknown_device_returns_404(
    authenticated_page: Page,
) -> None:
    """Asking for logs on a non-existent device yields a clean 404."""
    resp = authenticated_page.request.post(
        "/api/logs/requests",
        data={"device_id": "nonexistent-device-zzz"},
    )
    assert resp.status == 404, f"unexpected status: {resp.status} body={resp.text()[:400]}"


def test_request_logs_on_disconnected_device_queues_pending(
    authenticated_page: Page,
    simulator: SimulatorClient,
    logs_device: str,
) -> None:
    """When the target device is offline, the new endpoint still accepts
    the request (202) but the dispatch fails, so the row stays in a
    non-``sent`` state until the device reconnects."""
    page = authenticated_page
    device_id = logs_device

    simulator.force_offline(device_id, duration_sec=OFFLINE_DURATION_S)
    _wait_for_online(page, device_id, False, timeout=OFFLINE_DETECT_TIMEOUT_S)

    try:
        resp = page.request.post(
            "/api/logs/requests",
            data={"device_id": device_id},
            timeout=15_000,
        )
        assert resp.status == 202, (
            f"expected 202 while offline, got {resp.status}: {resp.text()[:400]}"
        )
        body = resp.json()
        # Dispatch failed (device offline) → status must NOT be "sent" right
        # away. It should be "pending" with a last_error recorded when we
        # poll.  Some scheduling may flip it before we read, but never to
        # "ready" without the device having replied.
        assert body.get("status") == "pending", (
            f"expected status=pending for offline device, got: {body!r}"
        )
        request_id = body["request_id"]

        row = page.request.get(f"/api/logs/requests/{request_id}").json()
        assert row.get("status") in ("pending", "sent"), (
            f"offline device log-request reached unexpected status: {row!r}"
        )
        assert row.get("status") != "ready"
    finally:
        # Make sure the device reconnects before downstream tests run.
        _wait_for_online(page, device_id, True, timeout=ONLINE_DETECT_TIMEOUT_S)
