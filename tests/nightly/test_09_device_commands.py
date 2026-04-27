"""Phase 9: UI -> CMS -> device WebSocket round-trip coverage (#250).

For every device-action button on the Devices page, we click through the real
UI, let the CMS issue its API call, and then assert the simulator received the
right WebSocket command. This proves the full pipeline:

    Playwright click -> CMS API route -> device_manager.send_to_device()
    -> WS frame -> agora/cms_client dispatch -> `_handle_*` fires

The simulator control plane's /recording endpoint (added in
agora-device-simulator PR #2) exposes the per-device inbound-command log that
we assert against.

Depends on phases 1-3 (stack + OOBE) and phase 4 (all 3 devices adopted).
"""

from __future__ import annotations

import re
import time
from typing import Any

import pytest
from playwright.sync_api import Page, expect

from tests.nightly.helpers.simulator import SimulatorClient


COMMAND_WAIT_S = 10.0


# ── helpers ──────────────────────────────────────────────────────────────


def _api_get(page: Page, path: str) -> Any:
    resp = page.request.get(path)
    assert resp.status == 200, f"GET {path} -> {resp.status}: {resp.text()[:500]}"
    return resp.json()


def _adopted_device_ids(page: Page) -> list[str]:
    return sorted(d["id"] for d in _api_get(page, "/api/devices") if d["status"] == "adopted")


def _wait_for_online(page: Page, device_id: str, timeout: float = 30.0) -> None:
    """Poll the CMS API until the device is reported online."""
    deadline = time.time() + timeout
    last: dict[str, Any] = {}
    while time.time() < deadline:
        last = _api_get(page, f"/api/devices/{device_id}")
        if last.get("is_online"):
            return
        time.sleep(0.5)
    raise AssertionError(
        f"device {device_id} not online after {timeout}s: {last!r}"
    )


def _expand_device_row(page: Page, device_id: str) -> None:
    """Ensure the device row is rendered and online before kebab actions.

    The action items inside the kebab are gated on ``d.is_online`` (server-
    rendered) and the live-update poll keeps them in sync. The kebab now
    lives directly on the collapsed row, so we no longer need to expand
    the detail TR — but we still wait for the row to surface and for the
    live state to flip online so the action buttons exist when we click.
    """
    _wait_for_online(page, device_id)
    page.goto("/devices")
    page.wait_for_load_state("networkidle")
    row = page.locator(f'tr.device-row[data-device-id="{device_id}"]').first
    expect(row).to_be_visible(timeout=10_000)
    # Wait for the actions cell live-update to settle so the kebab reflects
    # the current online state (Reboot/Factory Reset only show when online).
    actions_cell = page.locator(f'[data-live-actions="{device_id}"]').first
    expect(actions_cell).to_be_visible(timeout=15_000)


def _click_action(page: Page, device_id: str, button_text: str) -> None:
    """Click an action item in the row's kebab popover menu."""
    actions_cell = page.locator(f'[data-live-actions="{device_id}"]').first
    actions_cell.locator("button.btn-kebab").first.click()
    # Kebab items render in the top-layer popover. Wait briefly then click.
    page.locator(
        'div.kebab-menu[popover]:popover-open button',
        has_text=re.compile(rf"^{re.escape(button_text)}$"),
    ).first.click(timeout=5_000)


def _confirm_modal(page: Page) -> None:
    """Click the 'Confirm' button on the custom confirm modal overlay."""
    modal = page.locator(".modal-overlay").last
    expect(modal).to_be_visible(timeout=5_000)
    modal.locator("button.btn-danger", has_text="Confirm").click()


def _submit_prompt(page: Page, value: str) -> None:
    """Type a value into the prompt modal and click OK."""
    modal = page.locator(".modal-overlay").last
    expect(modal).to_be_visible(timeout=5_000)
    modal.locator("input.modal-input").first.fill(value)
    modal.locator("button.btn-primary", has_text="OK").click()


def _expect_api_response(
    page: Page, path_suffix: str, method: str = "POST",
    *, timeout: float = 15_000,
):
    """Context manager returning a ResponseInfo for the matching API call."""
    return page.expect_response(
        lambda r: path_suffix in r.url and r.request.method == method,
        timeout=timeout,
    )


# ── fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def first_adopted_device(authenticated_page: Page, simulator: SimulatorClient) -> str:
    """Pick the first adopted device and reset its recording for isolation."""
    simulator.wait_for_devices(expected_count=3, timeout=60.0)
    ids = _adopted_device_ids(authenticated_page)
    assert ids, "no adopted devices — test_03_devices must have run first"
    device_id = ids[0]
    simulator.reset_recording(device_id)
    return device_id


# ── tests ────────────────────────────────────────────────────────────────


def test_reboot_button_delivers_reboot_command_to_device(
    authenticated_page: Page,
    simulator: SimulatorClient,
    first_adopted_device: str,
) -> None:
    page = authenticated_page
    device_id = first_adopted_device

    _expand_device_row(page, device_id)
    with _expect_api_response(page, f"/api/devices/{device_id}/reboot"):
        _click_action(page, device_id, "Reboot")
        _confirm_modal(page)

    received = simulator.wait_for_command(device_id, "reboot", timeout=COMMAND_WAIT_S)
    assert len(received) == 1
    rec = simulator.get_recording(device_id)
    assert rec["counters"].get("reboot") == 1


def test_factory_reset_button_delivers_factory_reset_command(
    authenticated_page: Page,
    simulator: SimulatorClient,
    first_adopted_device: str,
) -> None:
    page = authenticated_page
    device_id = first_adopted_device

    _expand_device_row(page, device_id)
    with _expect_api_response(page, f"/api/devices/{device_id}/factory-reset"):
        _click_action(page, device_id, "Factory Reset")
        _confirm_modal(page)

    received = simulator.wait_for_command(device_id, "factory_reset", timeout=COMMAND_WAIT_S)
    assert len(received) == 1
    # Factory-reset will cause the simulator to wipe + (attempt to) reboot, which
    # makes the device go offline. Subsequent tests in the suite should not rely
    # on this specific device being healthy. Adoption for this device is reset
    # by the factory_reset handler on the sim side (see agora _handle_factory_reset).
    # We don't follow up here beyond asserting the message was delivered.


def test_toggle_ssh_button_delivers_config_with_ssh_enabled(
    authenticated_page: Page,
    simulator: SimulatorClient,
    first_adopted_device: str,
) -> None:
    """Toggle the SSH button and verify a config message with ssh_enabled arrives."""
    page = authenticated_page
    device_id = first_adopted_device

    # Read the current state so we know which direction the toggle will go.
    before = _api_get(page, f"/api/devices/{device_id}")
    target_state = not before.get("ssh_enabled", False)
    expected_button = "Enable SSH" if target_state else "Disable SSH"

    _expand_device_row(page, device_id)
    with _expect_api_response(page, f"/api/devices/{device_id}/ssh"):
        _click_action(page, device_id, expected_button)
        _confirm_modal(page)

    received = simulator.wait_for_command(device_id, "config", timeout=COMMAND_WAIT_S)
    ssh_configs = [c for c in received if "ssh_enabled" in c.get("payload", {})]
    assert ssh_configs, f"no config with ssh_enabled in {received!r}"
    assert ssh_configs[-1]["payload"]["ssh_enabled"] is target_state

    rec = simulator.get_recording(device_id)
    assert rec["last_config"].get("ssh_enabled") is target_state


def test_toggle_local_api_button_delivers_config_with_local_api_enabled(
    authenticated_page: Page,
    simulator: SimulatorClient,
    first_adopted_device: str,
) -> None:
    page = authenticated_page
    device_id = first_adopted_device

    before = _api_get(page, f"/api/devices/{device_id}")
    # local_api_enabled defaults to True (None also treated as enabled by the UI).
    current = before.get("local_api_enabled")
    currently_enabled = current is None or bool(current)
    target_state = not currently_enabled
    expected_button = "Enable Local API" if target_state else "Disable Local API"

    _expand_device_row(page, device_id)
    with _expect_api_response(page, f"/api/devices/{device_id}/local-api"):
        _click_action(page, device_id, expected_button)
        _confirm_modal(page)

    received = simulator.wait_for_command(device_id, "config", timeout=COMMAND_WAIT_S)
    matches = [c for c in received if "local_api_enabled" in c.get("payload", {})]
    assert matches, f"no config with local_api_enabled in {received!r}"
    assert matches[-1]["payload"]["local_api_enabled"] is target_state

    rec = simulator.get_recording(device_id)
    assert rec["last_config"].get("local_api_enabled") is target_state


def test_change_password_button_delivers_config_with_web_password(
    authenticated_page: Page,
    simulator: SimulatorClient,
    first_adopted_device: str,
) -> None:
    page = authenticated_page
    device_id = first_adopted_device
    new_password = "nightly-devpw-9a"

    _expand_device_row(page, device_id)
    with _expect_api_response(page, f"/api/devices/{device_id}/password"):
        _click_action(page, device_id, "Change Web Password")
        _submit_prompt(page, new_password)

    received = simulator.wait_for_command(device_id, "config", timeout=COMMAND_WAIT_S)
    pw_configs = [c for c in received if "web_password" in c.get("payload", {})]
    assert pw_configs, f"no config carrying web_password in {received!r}"
    # The password should be the literal new value (not a hash) in the ws payload:
    # the CMS ships the plaintext to the device which stores it locally. If
    # product decides to hash server-side, this assertion will need to adapt.
    assert pw_configs[-1]["payload"]["web_password"] == new_password

    rec = simulator.get_recording(device_id)
    assert rec["last_config"].get("web_password") == new_password
