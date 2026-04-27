"""Playwright tests for playback-interrupt confirmation dialogs."""

import asyncio
import threading

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.fake_device import FakeDevice


def _run_device_background(ws_url, device_id, mode="splash", asset=None):
    """Run a FakeDevice in a background thread, returning (ready_event, stop_event, thread).

    Keeps the device alive on a single event loop so that connect and
    disconnect happen on the same loop (avoids 'Future attached to a
    different loop' errors with websockets).
    """
    ready_event = threading.Event()
    stop_event = threading.Event()

    async def _device_loop():
        dev = FakeDevice(device_id, ws_url)
        await dev.connect()
        await dev.send_status(mode=mode, asset=asset)
        ready_event.set()
        while not stop_event.is_set():
            await asyncio.sleep(0.2)
        await dev.disconnect()

    thread = threading.Thread(target=lambda: asyncio.run(_device_loop()), daemon=True)
    thread.start()
    ready_event.wait(timeout=15)
    return stop_event, thread


def _adopt_device(page: Page, device_id: str):
    """Navigate to /devices and adopt a pending device."""
    page.goto("/devices")
    page.wait_for_load_state("domcontentloaded")
    row = page.locator(f'[data-device-id="{device_id}"]').first
    # Open the row's kebab menu to find the Adopt action (if any).
    adopt_kebab = row.locator(".btn-kebab")
    if adopt_kebab.count() > 0:
        adopt_kebab.click()
        menu = page.locator(".kebab-menu:popover-open")
        adopt_item = menu.get_by_role("menuitem", name="Adopt")
        if adopt_item.count() > 0:
            adopt_item.click()
            page.wait_for_load_state("networkidle")


def _click_reboot_via_kebab(page, device_id, timeout_ms=15000):
    """Trigger the reboot confirm modal for the given device.

    The kebab Reboot item is gated on d.is_online, which requires waiting
    for the FakeDevice's WS handshake to complete and propagate to the
    template render. To keep these tests focused on the confirm-modal
    behavior (not kebab gating), call rebootDevice() directly via JS —
    the same handler the menu item invokes.
    """
    # Fire-and-forget: rebootDevice is async (awaits the confirm modal),
    # so we MUST NOT return the promise — page.evaluate auto-awaits returned
    # promises and would hang forever waiting for the modal click.
    page.evaluate(
        "(id) => { window.rebootDevice(id, id); }",
        device_id,
    )


class TestPlaybackInterruptConfirm:
    """Actions on a playing device should show a playback-interrupt warning."""

    def test_reboot_playing_default_asset_no_warning(self, page: Page, ws_url, e2e_server):
        """Reboot confirm on a device playing its default asset (no schedule) should NOT
        include a playback warning — only active schedules count."""
        stop, thread = _run_device_background(ws_url, "play-reboot-001", mode="play", asset="video.mp4")
        try:
            _adopt_device(page, "play-reboot-001")

            page.goto("/devices")
            page.wait_for_load_state("domcontentloaded")

            # Open kebab and click Reboot from the popover menu (retries until online).
            _click_reboot_via_kebab(page, "play-reboot-001")

            # Confirm dialog should NOT mention "currently playing" — no schedule active
            modal = page.locator(".modal-overlay")
            expect(modal).to_be_visible(timeout=3000)
            expect(modal).not_to_contain_text("currently playing")

            # Cancel
            modal.locator("button", has_text="Cancel").click()
            expect(modal).to_be_hidden()
        finally:
            stop.set()
            thread.join(timeout=5)

    def test_reboot_idle_device_no_warning(self, page: Page, ws_url, e2e_server):
        """Reboot confirm on a non-playing device does NOT mention playback."""
        stop, thread = _run_device_background(ws_url, "idle-reboot-001", mode="splash")
        try:
            _adopt_device(page, "idle-reboot-001")

            page.goto("/devices")
            page.wait_for_load_state("domcontentloaded")

            _click_reboot_via_kebab(page, "idle-reboot-001")

            modal = page.locator(".modal-overlay")
            expect(modal).to_be_visible(timeout=3000)
            # Should NOT mention playing
            expect(modal).not_to_contain_text("currently playing")

            modal.locator("button", has_text="Cancel").click()
        finally:
            stop.set()
            thread.join(timeout=5)

    def test_data_playing_attribute_absent_when_no_schedule(self, page: Page, ws_url, e2e_server):
        """Device in play mode but with no active schedule should NOT have data-playing.

        A device playing its default asset (custom splash) reports mode='play',
        but this should not be treated as schedule-based playback.
        """
        stop, thread = _run_device_background(ws_url, "play-attr-001", mode="play", asset="test.mp4")
        try:
            _adopt_device(page, "play-attr-001")

            page.goto("/devices")
            page.wait_for_load_state("domcontentloaded")

            row = page.locator('tr.device-row[data-device-id="play-attr-001"]').first
            playing_attr = row.get_attribute("data-playing")
            assert playing_attr is None
        finally:
            stop.set()
            thread.join(timeout=5)

    def test_data_playing_attribute_absent_when_idle(self, page: Page, ws_url, e2e_server):
        """Device row should NOT have data-playing when device is not playing."""
        stop, thread = _run_device_background(ws_url, "idle-attr-001", mode="splash")
        try:
            _adopt_device(page, "idle-attr-001")

            page.goto("/devices")
            page.wait_for_load_state("domcontentloaded")

            row = page.locator('tr.device-row[data-device-id="idle-attr-001"]').first
            # Should not have data-playing attribute at all
            playing_attr = row.get_attribute("data-playing")
            assert playing_attr is None
        finally:
            stop.set()
            thread.join(timeout=5)
