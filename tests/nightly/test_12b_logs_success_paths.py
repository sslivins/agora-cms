"""Phase 9d follow-up: smoke coverage of both ``request_logs`` *success* paths.

``test_12_logs_roundtrip.py`` only exercises the failure path because the sim
container has no ``journalctl`` and the firmware reports an error. With the
sim shim added in agora-device-simulator PR #6, the simulator can now return
synthetic log content for any size we ask for, letting us exercise:

1. **Small JSON-over-WS branch** — payload fits inside ``LOGS_JSON_MAX_BYTES``
   (~900 KB), the firmware sends a single ``logs_response`` JSON frame, the
   CMS folds it into a tar.gz blob and marks the row ``ready``.
2. **Large HTTP-upload branch** — payload exceeds the JSON cap, the firmware
   builds a tar.gz and POSTs it to ``/api/devices/{id}/logs/{rid}/upload``
   (this path replaced the WPS-incompatible chunked-binary frames in
   agora#129).

Branch coverage is *proven*, not inferred: the sim shim increments
recorder counters (``logs_ws_json`` / ``logs_upload``) the firmware
actually took, so a future drift in the JSON-cap threshold can't silently
move a test from one branch to the other without us noticing.
"""

from __future__ import annotations

import io
import tarfile
import time
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.nightly.helpers.simulator import SimulatorClient


LOGS_POLL_TIMEOUT_S = 60.0
COUNTER_POLL_TIMEOUT_S = 30.0

# Stay comfortably under the firmware ``LOGS_JSON_MAX_BYTES`` (900_000) on the
# small-path test, and well over it on the large-path test so threshold
# drift can't accidentally cross the boundary.
SMALL_BYTES_PER_SERVICE = 5 * 1024            # 5 KB × 2 services -> ~10 KB
LARGE_BYTES_SINGLE_SERVICE = 2 * 1024 * 1024  # 2 MB single service -> well over 900 KB


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


def _wait_for_online(page: Page, device_id: str, timeout: float):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        d = _api_get(page, f"/api/devices/{device_id}")
        if d.get("is_online"):
            return d
        time.sleep(0.5)
    raise AssertionError(f"device {device_id} did not come online in {timeout}s")


def _poll_log_request_ready(page: Page, request_id: str, *, timeout: float) -> dict:
    deadline = time.monotonic() + timeout
    last: dict = {}
    while time.monotonic() < deadline:
        resp = page.request.get(f"/api/logs/requests/{request_id}")
        assert resp.status == 200, (
            f"GET /api/logs/requests/{request_id} -> {resp.status}: {resp.text()[:400]}"
        )
        last = resp.json()
        if last.get("status") == "ready":
            return last
        if last.get("status") == "failed":
            raise AssertionError(
                f"log request {request_id} failed: {last!r}"
            )
        time.sleep(1.0)
    raise AssertionError(
        f"log request {request_id} did not reach ready in {timeout}s: {last!r}"
    )


def _poll_counter(
    sim: SimulatorClient, serial: str, counter: str, *, timeout: float,
) -> int:
    """Block until the sim recorder reports counter >= 1, return its value."""
    deadline = time.monotonic() + timeout
    last_counters: dict = {}
    while time.monotonic() < deadline:
        rec = sim.get_recording(serial)
        last_counters = rec.get("counters", {})
        if last_counters.get(counter, 0) >= 1:
            return last_counters[counter]
        time.sleep(0.5)
    raise AssertionError(
        f"sim counter {counter!r} on {serial} did not increment in {timeout}s; "
        f"last counters: {last_counters!r}"
    )


def _download_and_unpack(page: Page, request_id: str) -> dict[str, bytes]:
    """Download the bundle and return ``{filename: bytes}``."""
    resp = page.request.get(f"/api/logs/requests/{request_id}/download")
    assert resp.status == 200, (
        f"download -> {resp.status}: {resp.text()[:400]}"
    )
    body = resp.body()
    assert body[:2] == b"\x1f\x8b", "downloaded bundle is not gzip"
    out: dict[str, bytes] = {}
    with tarfile.open(fileobj=io.BytesIO(body), mode="r:gz") as tf:
        for member in tf.getmembers():
            f = tf.extractfile(member)
            if f is not None:
                out[member.name] = f.read()
    return out


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def logs_success_device(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> str:
    simulator.wait_for_devices(expected_count=3, timeout=60.0)
    ids = _adopted_device_ids(authenticated_page)
    assert ids, "no adopted devices — test_03_devices must have run first"
    # Use the second adopted device. test_12 grabs ids[0] for its log
    # roundtrip; using a different one keeps the recording counters cleanly
    # partitioned and avoids fighting test_12's reset_recording calls.
    dev_id = ids[1] if len(ids) >= 2 else ids[0]
    _wait_for_online(authenticated_page, dev_id, timeout=30.0)
    simulator.reset_recording(dev_id)
    yield dev_id
    # Always clear the synth config so downstream tests see vanilla behaviour.
    try:
        simulator.clear_logs(dev_id)
    except Exception:
        pass


# ── tests ────────────────────────────────────────────────────────────────


def test_request_logs_small_payload_takes_ws_json_branch(
    authenticated_page: Page,
    simulator: SimulatorClient,
    logs_success_device: str,
) -> None:
    """Small synthesized payload -> firmware sends a single ``logs_response``
    JSON frame; CMS row reaches ``ready``; the bundle decompresses and
    contains the configured per-service content. Sim ``logs_ws_json``
    counter must fire (and ``logs_upload`` must NOT)."""
    page = authenticated_page
    device_id = logs_success_device

    services = ["agora-player", "agora-cms-client"]
    simulator.set_logs(device_id, {s: SMALL_BYTES_PER_SERVICE for s in services})

    resp = page.request.post(
        "/api/logs/requests",
        data={"device_id": device_id, "services": services, "since": "1h"},
        timeout=15_000,
    )
    assert resp.status == 202, (
        f"POST /api/logs/requests -> {resp.status}: {resp.text()[:400]}"
    )
    request_id = resp.json()["request_id"]

    final = _poll_log_request_ready(page, request_id, timeout=LOGS_POLL_TIMEOUT_S)
    assert final.get("status") == "ready"

    # Branch proof: the firmware took the JSON-over-WS path.
    ws_count = _poll_counter(
        simulator, device_id, "logs_ws_json", timeout=COUNTER_POLL_TIMEOUT_S,
    )
    assert ws_count >= 1
    rec = simulator.get_recording(device_id)
    assert rec.get("counters", {}).get("logs_upload", 0) == 0, (
        f"unexpected logs_upload on small payload: {rec!r}"
    )

    # Bundle contents match what we configured.
    files = _download_and_unpack(page, request_id)
    for s in services:
        name = f"{s}.log"
        assert name in files, f"missing {name} in bundle: {sorted(files)}"
        assert len(files[name]) == SMALL_BYTES_PER_SERVICE, (
            f"{name}: expected {SMALL_BYTES_PER_SERVICE} bytes, "
            f"got {len(files[name])}"
        )


def test_request_logs_large_payload_takes_http_upload_branch(
    authenticated_page: Page,
    simulator: SimulatorClient,
    logs_success_device: str,
) -> None:
    """Large synthesized payload (>LOGS_JSON_MAX_BYTES) -> firmware tar.gz's
    the logs and POSTs to ``/api/devices/{id}/logs/{rid}/upload``; CMS row
    reaches ``ready``. Sim ``logs_upload`` counter must fire (and
    ``logs_ws_json`` must NOT)."""
    page = authenticated_page
    device_id = logs_success_device

    services = ["agora-player"]
    simulator.set_logs(device_id, {services[0]: LARGE_BYTES_SINGLE_SERVICE})

    resp = page.request.post(
        "/api/logs/requests",
        data={"device_id": device_id, "services": services, "since": "1h"},
        timeout=15_000,
    )
    assert resp.status == 202, (
        f"POST /api/logs/requests -> {resp.status}: {resp.text()[:400]}"
    )
    request_id = resp.json()["request_id"]

    final = _poll_log_request_ready(page, request_id, timeout=LOGS_POLL_TIMEOUT_S)
    assert final.get("status") == "ready"

    # Branch proof: the firmware took the HTTP-upload path.
    upload_count = _poll_counter(
        simulator, device_id, "logs_upload", timeout=COUNTER_POLL_TIMEOUT_S,
    )
    assert upload_count >= 1
    rec = simulator.get_recording(device_id)
    assert rec.get("counters", {}).get("logs_ws_json", 0) == 0, (
        f"unexpected logs_ws_json on large payload: {rec!r}"
    )

    # Bundle contents match what we configured.
    files = _download_and_unpack(page, request_id)
    name = f"{services[0]}.log"
    assert name in files, f"missing {name} in bundle: {sorted(files)}"
    assert len(files[name]) == LARGE_BYTES_SINGLE_SERVICE, (
        f"{name}: expected {LARGE_BYTES_SINGLE_SERVICE} bytes, "
        f"got {len(files[name])}"
    )
