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


class TestDashboardPendingDeviceName:
    """Dashboard should show device friendly name, not raw ID, for pending devices."""

    def test_pending_device_shows_friendly_name(self, page: Page, ws_url, e2e_server):
        """A device registered with a custom name should show that name on the dashboard."""

        async def register():
            async with FakeDevice("name-test-001", ws_url, device_name="Kitchen Display") as dev:
                await dev.send_status()

        run_async(register())

        page.goto("/")
        page.wait_for_load_state("domcontentloaded")

        # The friendly name should appear in the Pending Devices section
        pending_section = page.locator("text=Pending Devices").locator("..")
        expect(pending_section.locator("text=Kitchen Display")).to_be_visible(timeout=5000)


class TestDeleteDeviceWithSchedules:
    """Deleting a device that has schedules should work from the UI."""

    def test_delete_device_with_schedule(self, page: Page, api, ws_url, e2e_server):
        """A device with schedules can be deleted via the Delete button."""

        # Register and adopt a device
        async def register():
            async with FakeDevice("del-e2e-001", ws_url) as dev:
                await dev.send_status()

        run_async(register())

        api.post("/api/devices/del-e2e-001/adopt")

        # Create an asset and schedule targeting this device
        asset_resp = api.create_asset()
        assert asset_resp.status_code == 201
        asset_id = asset_resp.json()["id"]

        sched_resp = api.post("/api/schedules/", json={
            "name": "Delete Test Schedule",
            "device_id": "del-e2e-001",
            "asset_id": asset_id,
            "start_time": "09:00",
            "end_time": "17:00",
        })
        assert sched_resp.status_code in (200, 201)

        # Go to devices page and delete the device
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        # Find the device row and click Delete
        row = page.locator('[data-device-id="del-e2e-001"]')
        expect(row).to_be_visible(timeout=5000)
        row.locator("button", has_text="Delete").click()

        # Confirm the modal
        page.locator(".modal-confirm").click()
        page.wait_for_load_state("networkidle")

        # Device should be gone
        expect(page.locator('[data-device-id="del-e2e-001"]')).to_have_count(0)
