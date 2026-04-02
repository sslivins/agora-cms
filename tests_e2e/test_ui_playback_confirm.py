"""Playwright tests for playback-interrupt confirmation dialogs."""

import asyncio
import threading

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async
from tests_e2e.fake_device import FakeDevice


def _register_and_adopt(ws_url: str, device_id: str, page: Page, mode: str = "splash", asset: str = None):
    """Register a device, adopt it, then send a status with the given mode."""

    async def _setup():
        dev = FakeDevice(device_id, ws_url)
        await dev.connect()
        await dev.send_status(mode=mode, asset=asset)
        return dev

    dev = run_async(_setup())

    # Adopt the device via API
    page.goto("/devices")
    page.wait_for_load_state("domcontentloaded")
    row = page.locator(f'[data-device-id="{device_id}"]').first
    adopt_btn = row.locator("button", has_text="Adopt")
    if adopt_btn.count() > 0:
        adopt_btn.click()
        page.wait_for_load_state("networkidle")

    return dev


class TestPlaybackInterruptConfirm:
    """Actions on a playing device should show a playback-interrupt warning."""

    def test_reboot_playing_device_shows_warning(self, page: Page, ws_url, e2e_server):
        """Reboot confirm on a playing device includes playback warning."""

        async def setup():
            dev = FakeDevice("play-reboot-001", ws_url)
            await dev.connect()
            await dev.send_status(mode="play", asset="video.mp4")
            return dev

        dev = run_async(setup())

        # Adopt
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        row = page.locator('[data-device-id="play-reboot-001"]').first
        adopt_btn = row.locator("button", has_text="Adopt")
        if adopt_btn.count() > 0:
            adopt_btn.click()
            page.wait_for_load_state("networkidle")

        # Send playing status again after adopt
        run_async(dev.send_status(mode="play", asset="video.mp4"))

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        # Expand device detail to find the Reboot button
        row = page.locator('[data-device-id="play-reboot-001"]').first
        row.click()

        # Scope to the visible detail row that follows the clicked row
        detail = page.locator('[data-detail-for="play-reboot-001"]').first
        reboot_btn = detail.locator('button', has_text="Reboot")
        expect(reboot_btn).to_be_visible(timeout=3000)
        reboot_btn.click()

        # Confirm dialog should mention "currently playing"
        modal = page.locator(".modal-overlay")
        expect(modal).to_be_visible(timeout=3000)
        expect(modal).to_contain_text("currently playing")

        # Cancel
        modal.locator("button", has_text="Cancel").click()
        expect(modal).to_be_hidden()

        run_async(dev.disconnect())

    def test_reboot_idle_device_no_warning(self, page: Page, ws_url, e2e_server):
        """Reboot confirm on a non-playing device does NOT mention playback."""

        async def setup():
            dev = FakeDevice("idle-reboot-001", ws_url)
            await dev.connect()
            await dev.send_status(mode="splash")
            return dev

        dev = run_async(setup())

        # Adopt
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        row = page.locator('[data-device-id="idle-reboot-001"]').first
        adopt_btn = row.locator("button", has_text="Adopt")
        if adopt_btn.count() > 0:
            adopt_btn.click()
            page.wait_for_load_state("networkidle")

        run_async(dev.send_status(mode="splash"))

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator('[data-device-id="idle-reboot-001"]').first
        row.click()

        detail = page.locator('[data-detail-for="idle-reboot-001"]').first
        reboot_btn = detail.locator('button', has_text="Reboot")
        expect(reboot_btn).to_be_visible(timeout=3000)
        reboot_btn.click()

        modal = page.locator(".modal-overlay")
        expect(modal).to_be_visible(timeout=3000)
        # Should NOT mention playing
        expect(modal).not_to_contain_text("currently playing")

        modal.locator("button", has_text="Cancel").click()

        run_async(dev.disconnect())

    def test_data_playing_attribute_set(self, page: Page, ws_url, e2e_server):
        """Device row should have data-playing='true' when device is playing."""

        async def setup():
            dev = FakeDevice("play-attr-001", ws_url)
            await dev.connect()
            await dev.send_status(mode="play", asset="test.mp4")
            return dev

        dev = run_async(setup())

        # Adopt
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        row = page.locator('[data-device-id="play-attr-001"]').first
        adopt_btn = row.locator("button", has_text="Adopt")
        if adopt_btn.count() > 0:
            adopt_btn.click()
            page.wait_for_load_state("networkidle")

        run_async(dev.send_status(mode="play", asset="test.mp4"))

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator('tr.device-row[data-device-id="play-attr-001"]').first
        expect(row).to_have_attribute("data-playing", "true")

        run_async(dev.disconnect())

    def test_data_playing_attribute_absent_when_idle(self, page: Page, ws_url, e2e_server):
        """Device row should NOT have data-playing when device is not playing."""

        async def setup():
            dev = FakeDevice("idle-attr-001", ws_url)
            await dev.connect()
            await dev.send_status(mode="splash")
            return dev

        dev = run_async(setup())

        # Adopt
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        row = page.locator('[data-device-id="idle-attr-001"]').first
        adopt_btn = row.locator("button", has_text="Adopt")
        if adopt_btn.count() > 0:
            adopt_btn.click()
            page.wait_for_load_state("networkidle")

        run_async(dev.send_status(mode="splash"))

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator('tr.device-row[data-device-id="idle-attr-001"]').first
        # Should not have data-playing attribute at all
        playing_attr = row.get_attribute("data-playing")
        assert playing_attr is None

        run_async(dev.disconnect())
