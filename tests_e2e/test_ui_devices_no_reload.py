"""Regression tests for the devices-page "no page reload" refactor.

Every action covered here used to call location.reload() on success, which
blew away scroll position, expanded group panels, etc. The tests verify the
DOM mutates in place by seeding a window-scoped sentinel before the action
and asserting (a) the URL doesn't change and (b) the sentinel survives.
"""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async, click_row_action
from tests_e2e.fake_device import FakeDevice


SENTINEL_SETUP = """
    () => {
        window.__noReloadSentinel = Math.random().toString(36).slice(2);
        return window.__noReloadSentinel;
    }
"""

SENTINEL_CHECK = "() => window.__noReloadSentinel"


def _install_sentinel(page: Page) -> str:
    """Install a window-scoped sentinel value. A full reload wipes it."""
    return page.evaluate(SENTINEL_SETUP)


def _assert_no_reload(page: Page, original_url: str, sentinel: str) -> None:
    """Assert the page didn't reload — sentinel survives, URL unchanged."""
    current = page.evaluate(SENTINEL_CHECK)
    assert current == sentinel, (
        f"Page reloaded: sentinel lost (was {sentinel!r}, now {current!r})"
    )
    assert page.url == original_url, (
        f"Page navigated: {original_url} -> {page.url}"
    )


def _register_and_adopt(api, ws_url, device_id, name=None):
    """Register a fake device and adopt it via the API."""

    async def register():
        async with FakeDevice(device_id, ws_url, device_name=name) as dev:
            await dev.send_status()

    run_async(register())
    api.post(f"/api/devices/{device_id}/adopt")


class TestDevicesNoReload:
    """Each covered action must update the DOM inline, not reload the page."""

    def test_delete_device_no_reload(self, page: Page, api, ws_url, e2e_server):
        """deleteDevice() should remove the row inline without reloading."""
        _register_and_adopt(api, ws_url, "nrl-del-001", "To Delete")
        _register_and_adopt(api, ws_url, "nrl-del-002", "To Keep")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        expect(page.locator('tr[data-device-id="nrl-del-001"]').first).to_be_visible(
            timeout=5000
        )

        original_url = page.url
        sentinel = _install_sentinel(page)

        # Trigger the Delete action via the row's kebab menu.
        row = page.locator('tr.device-row[data-device-id="nrl-del-001"]').first
        click_row_action(row, "Delete")

        confirm_modal = page.locator(".modal-overlay")
        expect(confirm_modal).to_be_visible(timeout=3000)
        confirm_modal.locator("button", has_text="Confirm").click()

        # The deleted row should disappear without a full reload.
        expect(page.locator('tr[data-device-id="nrl-del-001"]')).to_have_count(
            0, timeout=5000
        )
        # The other device should still be there.
        expect(
            page.locator('tr[data-device-id="nrl-del-002"]').first
        ).to_be_visible()

        _assert_no_reload(page, original_url, sentinel)

    def test_delete_device_decrements_group_count(
        self, page: Page, api, ws_url, e2e_server
    ):
        """Deleting a grouped device should drop the group-count badge by one."""
        _register_and_adopt(api, ws_url, "nrl-del-g-001", "Grouped A")
        _register_and_adopt(api, ws_url, "nrl-del-g-002", "Grouped B")

        resp = api.post("/api/devices/groups/", json={"name": "No-Reload Count Grp"})
        assert resp.status_code == 201
        group_id = resp.json()["id"]
        api.patch("/api/devices/nrl-del-g-001", json={"group_id": group_id})
        api.patch("/api/devices/nrl-del-g-002", json={"group_id": group_id})

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        badge = page.locator(f'[data-group-count="{group_id}"]')
        expect(badge).to_have_text("2 devices", timeout=5000)

        sentinel = _install_sentinel(page)
        original_url = page.url

        # The main "All Devices" table row is the one with a Delete action.
        row = page.locator(
            'table tr.device-row[data-device-id="nrl-del-g-001"]'
        ).first
        click_row_action(row, "Delete")
        confirm_modal = page.locator(".modal-overlay")
        expect(confirm_modal).to_be_visible(timeout=3000)
        confirm_modal.locator("button", has_text="Confirm").click()

        # Badge drops to 1 without a reload.
        expect(badge).to_have_text("1 device", timeout=5000)
        _assert_no_reload(page, original_url, sentinel)

    def test_create_group_no_reload(self, page: Page, api, ws_url, e2e_server):
        """createGroup() should render the new group-panel inline."""
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        sentinel = _install_sentinel(page)
        original_url = page.url

        page.fill("#group-name", "NoReload Group")
        page.fill("#group-desc", "Created without reload")
        page.click("#add-group-btn")

        # The new panel should appear without reloading.
        expect(page.locator("strong", has_text="NoReload Group")).to_be_visible(
            timeout=5000
        )
        _assert_no_reload(page, original_url, sentinel)

        # Inputs should be cleared and the button re-disabled.
        expect(page.locator("#group-name")).to_have_value("")
        expect(page.locator("#add-group-btn")).to_be_disabled()

    def test_delete_group_no_reload(self, page: Page, api, ws_url, e2e_server):
        """deleteGroup() should drop the panel inline and keep any devices."""
        _register_and_adopt(api, ws_url, "nrl-dg-001", "Keeper")

        resp = api.post("/api/devices/groups/", json={"name": "NoReload Del Group"})
        assert resp.status_code == 201
        group_id = resp.json()["id"]
        api.patch("/api/devices/nrl-dg-001", json={"group_id": group_id})

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        expect(
            page.locator("strong", has_text="NoReload Del Group")
        ).to_be_visible(timeout=5000)

        sentinel = _install_sentinel(page)
        original_url = page.url

        group_panel = page.locator(
            f'div.group-panel[data-group-id="{group_id}"]'
        )
        group_panel.locator(".group-actions .btn-kebab").click()
        page.locator(".kebab-menu:popover-open").get_by_role(
            "menuitem", name="Delete"
        ).click()
        confirm_modal = page.locator(".modal-overlay")
        expect(confirm_modal).to_be_visible(timeout=3000)
        confirm_modal.locator("button", has_text="Confirm").click()

        # Group panel should disappear inline; device should still be on the page.
        expect(page.locator("strong", has_text="NoReload Del Group")).to_have_count(
            0, timeout=5000
        )
        expect(
            page.locator('[data-device-id="nrl-dg-001"]').first
        ).to_be_visible()
        _assert_no_reload(page, original_url, sentinel)

    def test_check_for_updates_no_reload(self, page: Page, api, ws_url, e2e_server):
        """checkForUpdates() should refresh inline via the poller hook."""
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        menu_item = page.locator("#check-updates-btn")
        if menu_item.count() == 0:
            pytest.skip("Check for updates menu item not exposed to this user")

        sentinel = _install_sentinel(page)
        original_url = page.url

        # The action lives inside a kebab popover on the Devices card header.
        # Open it first, then invoke the menu item.
        page.locator(".card .btn-kebab").first.click()
        page.locator(".kebab-menu:popover-open").get_by_role(
            "menuitem", name="Check for updates"
        ).click()
        # Let the request settle — we toast on success and re-enable the item.
        page.wait_for_timeout(500)
        _assert_no_reload(page, original_url, sentinel)
