"""Phase 9d: Logs round-trip — ``POST /api/devices/{id}/logs`` (#250).

Covers the end-to-end path for the "Get Logs" UI action:

1. Happy-ish path — CMS forwards the ``request_logs`` command over WS and
   the simulator records it with ``services`` and ``since`` as sent. The
   simulator container has no ``journalctl`` binary, so the device
   responds with ``error="journalctl not available on this device"``;
   the CMS router turns that into a 502 with the device's error string.
   Either way, proof of delivery is the recorded command on the sim.

2. Unknown device — ``POST /api/devices/unknown/logs`` returns 404 from
   the ``_get_device_with_access`` guard.

3. Disconnected device — forcing the target offline first makes the
   endpoint short-circuit with 409 (``device not connected``) rather
   than timing out.

These tests are intentionally forgiving about the *content* of the
successful response: the production path relies on a real ``journalctl``
that isn't present in the sim container, so reaching a clean 200 would
require either modifying the sim or seeding fake logs. Shipping end-to-end
WS delivery coverage here; fake-logs fixture can follow later if needed.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.nightly.helpers.simulator import SimulatorClient


LOGS_RESPONSE_TIMEOUT_S = 45.0
OFFLINE_DURATION_S = 15.0
OFFLINE_DETECT_TIMEOUT_S = 30.0
ONLINE_DETECT_TIMEOUT_S = 45.0


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
    return dev_id


# ── tests ────────────────────────────────────────────────────────────────


def test_request_logs_delivers_request_logs_command_to_device(
    authenticated_page: Page,
    simulator: SimulatorClient,
    logs_device: str,
) -> None:
    """POST /api/devices/{id}/logs -> the simulator records a
    ``request_logs`` command containing request_id, services, since."""
    page = authenticated_page
    device_id = logs_device

    services = ["agora-player", "agora-cms-client"]
    since = "1h"
    resp = page.request.post(
        f"/api/devices/{device_id}/logs",
        data={"services": services, "since": since},
        timeout=LOGS_RESPONSE_TIMEOUT_S * 1000,
    )

    # Simulator has no journalctl; expect either 200 (fake logs somehow
    # returned) or 502 (device reported an error). Never a raw 500.
    assert resp.status in (200, 502), (
        f"POST /api/devices/{device_id}/logs -> {resp.status}: {resp.text()[:400]}"
    )
    if resp.status == 502:
        detail = (resp.json() or {}).get("detail", "")
        assert "journalctl" in detail.lower() or "not available" in detail.lower(), (
            f"expected device-side error message, got: {detail!r}"
        )

    # The simulator must have recorded the command irrespective of the
    # eventual response — this is the real proof-of-delivery.
    received = simulator.wait_for_command(
        device_id, "request_logs", timeout=LOGS_RESPONSE_TIMEOUT_S,
    )
    payload = received.get("payload") or {}
    assert payload.get("type") == "request_logs"
    assert payload.get("services") == services
    assert payload.get("since") == since
    assert isinstance(payload.get("request_id"), str) and payload["request_id"]


def test_request_logs_unknown_device_returns_404(
    authenticated_page: Page,
) -> None:
    """Asking for logs on a non-existent device yields a clean 404."""
    resp = authenticated_page.request.post(
        "/api/devices/nonexistent-device-zzz/logs",
        data={},
    )
    assert resp.status == 404, f"unexpected status: {resp.status} body={resp.text()[:400]}"


def test_request_logs_on_disconnected_device_returns_409(
    authenticated_page: Page,
    simulator: SimulatorClient,
    logs_device: str,
) -> None:
    """When the target device is offline, the endpoint short-circuits with
    409 from the ``is_connected`` guard rather than waiting for a timeout."""
    page = authenticated_page
    device_id = logs_device

    simulator.force_offline(device_id, duration_sec=OFFLINE_DURATION_S)
    _wait_for_online(page, device_id, False, timeout=OFFLINE_DETECT_TIMEOUT_S)

    try:
        resp = page.request.post(
            f"/api/devices/{device_id}/logs",
            data={},
            timeout=15_000,
        )
        assert resp.status == 409, (
            f"expected 409 from disconnected device, got {resp.status}: {resp.text()[:400]}"
        )
    finally:
        # Make sure the device reconnects before downstream tests run.
        _wait_for_online(page, device_id, True, timeout=ONLINE_DETECT_TIMEOUT_S)
