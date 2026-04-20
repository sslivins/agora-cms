"""Phase 4: device adoption (#250).

By the time this phase runs, the simulator container has registered three
devices with the CMS via the `/ws/device` WebSocket. They show up on the
Devices page as PENDING. This test walks the adoption flow:

- device #1 is adopted through the **real UI** (click Adopt, fill the
  modal: name + profile + location + group=None, submit)
- devices #2 and #3 are adopted via the underlying `POST /api/devices/{id}/adopt`
  endpoint (faster, still validates the API contract)
- afterwards we assert:
    * each device's `status == "adopted"` and `is_online is True` via the
      CMS API (sourced from live WebSocket state + DB)
    * the simulator control plane still reports `ws_open: True` for them
      (adoption must not disconnect the devices)
    * one device got the custom name/location chosen in the UI modal

Depends on phases 1-3 having set up the stack + walked the OOBE wizard.
"""

from __future__ import annotations

import re
import time
from typing import Any

import pytest
from playwright.sync_api import Page, TimeoutError as PwTimeoutError, expect

from tests.nightly.helpers.simulator import SimulatorClient


ADOPTION_POLL_TIMEOUT_S = 30.0
ADOPTION_POLL_INTERVAL_S = 0.5

# Custom values we apply to device #1 via the UI — they should round-trip
# to the API representation afterwards.
UI_DEVICE_NAME = "Lobby Display"
UI_DEVICE_LOCATION = "HQ Main Lobby"


# ── helpers ──────────────────────────────────────────────────────────────


def _api_get(page: Page, path: str) -> Any:
    resp = page.request.get(path)
    assert resp.status == 200, f"GET {path} -> {resp.status}: {resp.text()[:500]}"
    return resp.json()


def _get_device(page: Page, device_id: str) -> dict[str, Any]:
    return _api_get(page, f"/api/devices/{device_id}")


def _wait_for_device_status(
    page: Page, device_id: str, status: str,
    *, online: bool | None = None,
    timeout: float = ADOPTION_POLL_TIMEOUT_S,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = _get_device(page, device_id)
        if last.get("status") == status and (online is None or last.get("is_online") == online):
            return last
        time.sleep(ADOPTION_POLL_INTERVAL_S)
    raise AssertionError(
        f"device {device_id} did not reach status={status!r} "
        f"(online={online}) within {timeout}s. Last: {last!r}"
    )


def _first_pending_device_serial(page: Page, simulator: SimulatorClient) -> list[str]:
    """Intersection of the simulator's serials with the CMS's pending list.

    The CMS may have leftover devices from previous partial runs (shouldn't,
    since the stack fixture tears down with `-v`, but be defensive) — so we
    cross-reference against the simulator's ground-truth list of serials.
    """
    sim_serials = set(simulator.serials())
    assert len(sim_serials) == 3, f"expected 3 sim devices, got {sim_serials!r}"

    deadline = time.monotonic() + ADOPTION_POLL_TIMEOUT_S
    while time.monotonic() < deadline:
        cms_devices = _api_get(page, "/api/devices")
        by_id = {d["id"]: d for d in cms_devices}
        pending = [
            sid for sid in sim_serials
            if by_id.get(sid, {}).get("status") == "pending"
        ]
        if len(pending) == 3:
            return sorted(pending)
        time.sleep(ADOPTION_POLL_INTERVAL_S)

    raise AssertionError(
        f"expected 3 pending devices matching simulator serials, got "
        f"{[by_id.get(s, {}).get('status') for s in sim_serials]!r}"
    )


def _first_profile_id(page: Page) -> str:
    profiles = _api_get(page, "/api/profiles")
    assert profiles, "no device profiles seeded"
    # Prefer pi-5 since that's what the simulator reports itself as
    # (AGORA_SIM_BOARD=pi_5 in the compose overlay).
    preferred = [p for p in profiles if p["name"] == "pi-5"]
    return (preferred or profiles)[0]["id"]


# ── tests ─────────────────────────────────────────────────────────────────


def test_pending_devices_listed_on_devices_page(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    """Sanity: /devices renders rows for all 3 simulator devices with Pending."""
    # Make sure the simulator side is fully up before we navigate.
    simulator.wait_for_devices(expected_count=3, timeout=60.0)

    serials = _first_pending_device_serial(authenticated_page, simulator)
    assert len(serials) == 3

    page = authenticated_page
    page.goto("/devices")
    page.wait_for_load_state("domcontentloaded")

    for serial in serials:
        row = page.locator(f'tr.device-row[data-device-id="{serial}"]').first
        expect(row).to_be_visible(timeout=10_000)
        # Status cell carries `data-live-status="<serial>"`.
        status_cell = row.locator(f'[data-live-status="{serial}"]')
        expect(status_cell).to_have_attribute("data-device-status", "pending")
        # An Adopt button is present for pending devices.
        # An Adopt action is present in the row's kebab menu for pending devices.
        expect(row.locator('button[role="menuitem"]', has_text="Adopt")).to_have_count(1)


def test_adopt_first_device_through_ui(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    """Click the Adopt button, fill the modal, submit, verify state change."""
    simulator.wait_for_devices(expected_count=3, timeout=60.0)
    serials = _first_pending_device_serial(authenticated_page, simulator)
    target = serials[0]

    page = authenticated_page
    page.goto("/devices")
    page.wait_for_load_state("domcontentloaded")

    row = page.locator(f'tr.device-row[data-device-id="{target}"]').first
    expect(row).to_be_visible(timeout=10_000)
    # Open the row's kebab menu and click the Adopt action.
    row.locator('.btn-kebab').click()
    page.locator('.kebab-menu:popover-open').get_by_role('menuitem', name='Adopt').click()

    # ── Adoption modal ──────────────────────────────────────────────────
    # Modal is dynamically constructed by showAdoptModal() in app.js;
    # fields in order: name (input), profile (select), location (input),
    # group (select).
    modal = page.locator(".modal-overlay").last
    expect(modal).to_be_visible(timeout=5_000)
    expect(modal.locator("h3", has_text="Adopt Device")).to_be_visible()

    name_input = modal.locator("input.modal-input").nth(0)
    profile_select = modal.locator("select.modal-input").nth(0)
    location_input = modal.locator("input.modal-input").nth(1)
    group_select = modal.locator("select.modal-input").nth(1)

    # Name is pre-filled with the device id; replace it.
    name_input.fill(UI_DEVICE_NAME)
    # Pick the first non-placeholder profile option by value.
    profile_id = _first_profile_id(page)
    profile_select.select_option(value=profile_id)
    location_input.fill(UI_DEVICE_LOCATION)
    # Leave group=None (default).

    submit = modal.locator('button.btn-primary', has_text="Adopt")
    expect(submit).to_be_enabled()
    # After submit the page reloads (location.reload in adoptDevice), so
    # capture the POST response before the tear-down happens.
    with page.expect_response(
        lambda r: f"/api/devices/{target}/adopt" in r.url and r.request.method == "POST",
        timeout=15_000,
    ) as rinfo:
        submit.click()
    resp = rinfo.value
    # Response status is captured before page reload; body may be unreadable
    # afterwards (Playwright clears network buffers on navigation), so we
    # trust the status here and verify the adoption outcome via follow-up
    # API calls below.
    assert resp.status == 200, f"adopt failed: {resp.status}"

    # Wait for the page reload to finish, then verify via API.
    try:
        page.wait_for_load_state("domcontentloaded", timeout=10_000)
    except PwTimeoutError:
        pass  # reload may have already completed

    adopted = _wait_for_device_status(page, target, "adopted", online=True)
    assert adopted["name"] == UI_DEVICE_NAME, adopted
    assert adopted["location"] == UI_DEVICE_LOCATION, adopted
    assert adopted["group_id"] in (None, ""), adopted


def test_adopt_remaining_devices_via_api(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    """Adopt the other two simulator devices through POST /api/devices/{id}/adopt."""
    page = authenticated_page
    simulator.wait_for_devices(expected_count=3, timeout=60.0)

    # Re-read the CMS's view — some devices should now show as adopted from
    # the previous test. We want whatever is still pending.
    cms_devices = _api_get(page, "/api/devices")
    pending = [d["id"] for d in cms_devices if d["status"] == "pending"]
    assert len(pending) == 2, (
        f"expected 2 pending devices after UI-adoption test, got {pending!r}"
    )

    profile_id = _first_profile_id(page)

    for idx, device_id in enumerate(sorted(pending)):
        body = {
            "name": f"Nightly Device {idx + 2}",
            "profile_id": profile_id,
            "location": f"Nightly Rack {idx + 2}",
        }
        resp = page.request.post(f"/api/devices/{device_id}/adopt", data=body)
        assert resp.status == 200, (
            f"adopt API for {device_id} -> {resp.status}: {resp.text()[:400]}"
        )
        assert resp.json() == {"ok": True}

        adopted = _wait_for_device_status(page, device_id, "adopted", online=True)
        assert adopted["name"] == body["name"], adopted
        assert adopted["location"] == body["location"], adopted


def test_all_devices_adopted_and_still_connected(
    authenticated_page: Page,
    simulator: SimulatorClient,
) -> None:
    """Cross-check: CMS says adopted+online, simulator says ws_open.

    Catches regressions like "adoption closes the WebSocket" or "status flag
    not persisted".
    """
    page = authenticated_page
    sim_devices = simulator.wait_for_devices(expected_count=3, timeout=60.0)
    sim_serials = {d["serial"]: d for d in sim_devices}

    cms_devices = _api_get(page, "/api/devices")
    cms_by_id = {d["id"]: d for d in cms_devices}

    for serial, sim_d in sim_serials.items():
        assert sim_d["ws_open"] is True, (
            f"simulator reports device {serial} disconnected after adoption: {sim_d!r}"
        )

        cms_d = cms_by_id.get(serial)
        assert cms_d, f"device {serial} missing from CMS listing"
        assert cms_d["status"] == "adopted", cms_d
        # is_online is live state; occasionally takes a heartbeat to settle.
        if not cms_d.get("is_online"):
            cms_d = _wait_for_device_status(page, serial, "adopted", online=True)
        assert cms_d["is_online"] is True, cms_d
        # last_seen recent-ish
        assert cms_d.get("last_seen"), cms_d

    # And the devices page should now render the Adopted badge for all 3.
    page.goto("/devices")
    page.wait_for_load_state("domcontentloaded")
    for serial in sim_serials:
        cell = page.locator(f'[data-live-status="{serial}"]').first
        # data-device-status reflects the persisted DB status.
        expect(cell).to_have_attribute("data-device-status", "adopted", timeout=10_000)
