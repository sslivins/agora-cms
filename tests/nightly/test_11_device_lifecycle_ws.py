"""Phase 9c: WS lifecycle — forced offline + auto-reconnect (#250).

Covers the run-loop around the device's WS connection:

1. Drop the WS with ``POST /devices/{serial}/offline`` → CMS flips
   ``is_online`` to False within a reasonable window.
2. After the simulated outage window, the device auto-reconnects and CMS
   reports online again.
3. On reconnect the CMS pushes a fresh ``sync`` frame (the adoption path
   in ``cms/routers/ws.py`` calls ``build_device_sync`` for every
   newly-connected adopted device).
4. A config change made *while* the device was offline (a timezone flip)
   is visible in the post-reconnect sync — proving the CMS reads current
   state from the DB rather than relying on a queued message.

These tests drive the offline signal via the simulator fault-injection
endpoint rather than killing the container — that way the per-device
instance restores itself cleanly and the rest of the suite sees a
healthy fleet.

Depends on test_01_oobe + test_03_devices adopting all simulator devices.
"""

from __future__ import annotations

import time
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.nightly.helpers.simulator import SimulatorClient


# How long to simulate the outage. Must be long enough for the CMS WS server
# to detect the socket close, but short enough that waiting for reconnect
# doesn't slow the suite noticeably. 5s covers both in practice.
OFFLINE_DURATION_S = 5.0

# CMS detects the WS close more or less synchronously, but give the event
# loop a generous window so flaky networks (macOS GH runners especially)
# don't produce spurious failures.
OFFLINE_DETECT_TIMEOUT_S = 30.0
ONLINE_DETECT_TIMEOUT_S = 45.0
SYNC_WAIT_S = 20.0


# ── helpers ──────────────────────────────────────────────────────────────


def _api_get(page: Page, path: str) -> Any:
    resp = page.request.get(path)
    assert resp.status == 200, f"GET {path} -> {resp.status}: {resp.text()[:400]}"
    return resp.json()


def _api_patch(page: Page, path: str, body: dict, *, expected: int = 200) -> Any:
    resp = page.request.patch(path, data=body)
    assert resp.status == expected, (
        f"PATCH {path} -> {resp.status} (expected {expected}): {resp.text()[:400]}"
    )
    return resp.json()


def _adopted_device_ids(page: Page) -> list[str]:
    return sorted(
        d["id"] for d in _api_get(page, "/api/devices")
        if d.get("status") == "adopted"
    )


def _wait_for_online_state(
    page: Page, device_id: str, expected: bool,
    *, timeout: float,
) -> dict:
    """Poll /api/devices/{id} until is_online matches expected. Returns the
    latest device snapshot."""
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


def _wait_for_sync(
    simulator: SimulatorClient,
    device_id: str,
    *,
    since_count: int,
    timeout: float = SYNC_WAIT_S,
    predicate=None,
) -> dict:
    """Wait for a sync *newer* than the recording's existing sync count.

    ``since_count`` is the number of sync commands that had already been
    recorded before the event we care about — any sync logged beyond that
    index is a new one.
    """
    deadline = time.monotonic() + timeout
    last = {}
    while time.monotonic() < deadline:
        rec = simulator.get_recording(device_id)
        last = rec
        syncs = [c for c in rec.get("commands", []) if c.get("type") == "sync"]
        if len(syncs) > since_count:
            for cmd in syncs[since_count:]:
                if predicate is None or predicate(cmd.get("payload") or {}):
                    return cmd
        time.sleep(0.25)
    raise AssertionError(
        f"no matching sync for {device_id} (since_count={since_count}) "
        f"within {timeout}s. Last recording: {last!r}"
    )


def _sync_count(simulator: SimulatorClient, device_id: str) -> int:
    rec = simulator.get_recording(device_id)
    return sum(1 for c in rec.get("commands", []) if c.get("type") == "sync")


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def lifecycle_device(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> str:
    """Pick a device that is currently online and has finished early-boot
    adoption traffic, so that offline/reconnect tests have a stable start."""
    simulator.wait_for_devices(expected_count=3, timeout=60.0)
    ids = _adopted_device_ids(authenticated_page)
    assert ids, "no adopted devices — test_03_devices must have run first"
    # Use the LAST adopted device to avoid overlap with test_08 (Group A
    # pinning sim-00000) and test_09/10 (first_adopted_device).
    dev_id = ids[-1]
    _wait_for_online_state(authenticated_page, dev_id, True, timeout=30.0)
    return dev_id


# ── tests ────────────────────────────────────────────────────────────────


def test_force_offline_flips_is_online_false(
    authenticated_page: Page,
    simulator: SimulatorClient,
    lifecycle_device: str,
) -> None:
    """Forcing the simulator offline closes the WS; CMS should drop the
    connection entry and start reporting ``is_online=False``."""
    page = authenticated_page
    device_id = lifecycle_device

    assert _api_get(page, f"/api/devices/{device_id}")["is_online"] is True

    simulator.force_offline(device_id, duration_sec=OFFLINE_DURATION_S)

    snap = _wait_for_online_state(
        page, device_id, False, timeout=OFFLINE_DETECT_TIMEOUT_S,
    )
    assert snap["is_online"] is False, snap


def test_device_reconnects_after_offline_window_expires(
    authenticated_page: Page,
    simulator: SimulatorClient,
    lifecycle_device: str,
) -> None:
    """Once the simulator's offline window ends the device reconnects and
    CMS reports it online again."""
    page = authenticated_page
    device_id = lifecycle_device

    simulator.force_offline(device_id, duration_sec=OFFLINE_DURATION_S)
    _wait_for_online_state(page, device_id, False, timeout=OFFLINE_DETECT_TIMEOUT_S)

    snap = _wait_for_online_state(
        page, device_id, True, timeout=ONLINE_DETECT_TIMEOUT_S,
    )
    assert snap["is_online"] is True, snap


def test_reconnect_pushes_fresh_sync_with_current_config(
    authenticated_page: Page,
    simulator: SimulatorClient,
    lifecycle_device: str,
) -> None:
    """A mutation performed *while* the device is offline must land on
    the device via the sync frame the CMS sends on reconnect.

    Walk:
      1. Snapshot sync count.
      2. Force offline.
      3. While offline, PATCH the device's timezone.
      4. Wait for the offline window to pass and the device to reconnect.
      5. A new sync should be recorded carrying the updated timezone.
    """
    page = authenticated_page
    device_id = lifecycle_device

    before_snapshot = _api_get(page, f"/api/devices/{device_id}")
    original_tz = before_snapshot.get("timezone") or "UTC"
    target_tz = "Asia/Tokyo" if original_tz != "Asia/Tokyo" else "Pacific/Auckland"

    baseline_syncs = _sync_count(simulator, device_id)

    try:
        simulator.force_offline(device_id, duration_sec=OFFLINE_DURATION_S)
        _wait_for_online_state(
            page, device_id, False, timeout=OFFLINE_DETECT_TIMEOUT_S,
        )

        # CMS should refuse to send immediate sync while the device is
        # disconnected (send_to_device returns False silently). We still
        # mutate the row so reconnect picks it up.
        _api_patch(page, f"/api/devices/{device_id}", {"timezone": target_tz})

        _wait_for_online_state(
            page, device_id, True, timeout=ONLINE_DETECT_TIMEOUT_S,
        )

        new_sync = _wait_for_sync(
            simulator, device_id,
            since_count=baseline_syncs,
            predicate=lambda p: p.get("timezone") == target_tz,
        )
        assert new_sync["payload"]["timezone"] == target_tz
    finally:
        # Restore tz so other phases don't see shifted schedule windows.
        _wait_for_online_state(
            page, device_id, True, timeout=ONLINE_DETECT_TIMEOUT_S,
        )
        _api_patch(page, f"/api/devices/{device_id}", {"timezone": original_tz})
