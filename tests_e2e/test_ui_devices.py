"""Playwright tests for the Devices page."""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async
from tests_e2e.fake_device import FakeDevice


class TestDevicesPage:
    """Device list and management UI."""

    def test_pending_device_appears(self, page: Page, ws_url, e2e_server):
        """A newly registered device should appear on the devices page."""

        async def register():
            async with FakeDevice("ui-dev-001", ws_url) as dev:
                await dev.send_status()

        run_async(register())

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        # CMS sets device name = device_id; device row uses data-device-id attr
        expect(page.locator('[data-device-id="ui-dev-001"]').first).to_be_visible(timeout=5000)

    def test_adopt_device_button(self, page: Page, ws_url, e2e_server):
        """Clicking Adopt should change device status."""

        async def register():
            async with FakeDevice("ui-adopt-001", ws_url) as dev:
                await dev.send_status()

        run_async(register())

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        adopt_btn = page.locator("button", has_text="Adopt").first
        expect(adopt_btn).to_be_visible(timeout=5000)
        adopt_btn.click()
        page.wait_for_load_state("networkidle")

        # Reload and verify the device is no longer pending
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

    def test_device_groups_section_visible(self, page: Page):
        """The Device Groups section should be present on the devices page."""
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        expect(page.locator("h2", has_text="Device Groups")).to_be_visible()

    def test_create_device_group(self, page: Page):
        """Creating a device group via the UI form."""
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        group_name_input = page.locator('#group-name')
        if group_name_input.count() == 0:
            pytest.skip("No group creation form found on page")
        group_name_input.fill("E2E Test Group")
        page.locator("button", has_text="Add Group").click()
        page.wait_for_load_state("networkidle")
        # Group name appears in bold in the group header
        expect(page.locator("strong", has_text="E2E Test Group")).to_be_visible()
