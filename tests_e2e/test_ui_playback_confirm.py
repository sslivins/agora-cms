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
    adopt_btn = row.locator("button", has_text="Adopt")
    if adopt_btn.count() > 0:
        adopt_btn.click()
        page.wait_for_load_state("networkidle")


class TestPlaybackInterruptConfirm:
    """Actions on a playing device should show a playback-interrupt warning."""

    def test_reboot_playing_device_shows_warning(self, page: Page, ws_url, e2e_server):
        """Reboot confirm on a playing device includes playback warning."""
        stop, thread = _run_device_background(ws_url, "play-reboot-001", mode="play", asset="video.mp4")
        try:
            _adopt_device(page, "play-reboot-001")

            page.goto("/devices")
            page.wait_for_load_state("domcontentloaded")

            # Expand device detail to reveal the Reboot button
            row = page.locator('[data-device-id="play-reboot-001"]').first
            expect(row).to_be_visible(timeout=5000)
            row.locator(".expand-toggle").click()

            detail = page.locator('[data-detail-for="play-reboot-001"]').first
            expect(detail).to_be_visible(timeout=3000)
            reboot_btn = detail.locator("button", has_text="Reboot")
            expect(reboot_btn).to_be_visible(timeout=3000)
            reboot_btn.click()

            # Confirm dialog should mention "currently playing"
            modal = page.locator(".modal-overlay")
            expect(modal).to_be_visible(timeout=3000)
            expect(modal).to_contain_text("currently playing")

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

            row = page.locator('[data-device-id="idle-reboot-001"]').first
            expect(row).to_be_visible(timeout=5000)
            row.locator(".expand-toggle").click()

            detail = page.locator('[data-detail-for="idle-reboot-001"]').first
            expect(detail).to_be_visible(timeout=3000)
            reboot_btn = detail.locator("button", has_text="Reboot")
            expect(reboot_btn).to_be_visible(timeout=3000)
            reboot_btn.click()

            modal = page.locator(".modal-overlay")
            expect(modal).to_be_visible(timeout=3000)
            # Should NOT mention playing
            expect(modal).not_to_contain_text("currently playing")

            modal.locator("button", has_text="Cancel").click()
        finally:
            stop.set()
            thread.join(timeout=5)

    def test_data_playing_attribute_set(self, page: Page, ws_url, e2e_server):
        """Device row should have data-playing='true' when device is playing."""
        stop, thread = _run_device_background(ws_url, "play-attr-001", mode="play", asset="test.mp4")
        try:
            _adopt_device(page, "play-attr-001")

            page.goto("/devices")
            page.wait_for_load_state("domcontentloaded")

            row = page.locator('tr.device-row[data-device-id="play-attr-001"]').first
            expect(row).to_have_attribute("data-playing", "true")
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
