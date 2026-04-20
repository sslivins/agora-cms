"""E2E tests for consolidated kebab (⋮) action menus — issue #249.

Covers the native-HTML-popover-based kebab:
- Opening a kebab reveals the popover menu (in top-layer).
- Outside click closes the menu.
- Escape closes the menu.
- Opening one kebab auto-closes any other open one (popover=auto behavior).
- Destructive items carry the `kebab-item-danger` class.
- Every table that had inline row actions now has a kebab.

Uses the /profiles page for most open/close behavior tests since built-in
profiles are seeded by the server at startup and require no test fixtures.
"""

import pytest
from playwright.sync_api import Page, expect


def _open_menu(page: Page):
    """Return the currently-open popover kebab menu locator."""
    return page.locator(".kebab-menu:popover-open")


@pytest.mark.e2e
class TestKebabMenuBehavior:
    """Shared ⋮ menu open/close and a11y behavior."""

    def test_kebab_opens_and_closes_on_outside_click(self, page: Page, e2e_server):
        """Clicking outside an open kebab closes it."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has=page.locator(".badge-builtin")).first
        kebab = row.locator(".btn-kebab")

        # Initially no popover open.
        expect(_open_menu(page)).to_have_count(0)

        # Opening the kebab exposes the popover menu.
        kebab.click()
        menu = _open_menu(page)
        expect(menu).to_have_count(1)
        expect(menu.get_by_role("menuitem", name="Edit")).to_be_visible()
        expect(kebab).to_have_attribute("aria-expanded", "true")

        # Click outside — popover light-dismisses.
        page.locator("h1").click()
        expect(_open_menu(page)).to_have_count(0)
        expect(kebab).to_have_attribute("aria-expanded", "false")

    def test_kebab_closes_on_escape(self, page: Page, e2e_server):
        """Pressing Escape closes any open kebab menu."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has=page.locator(".badge-builtin")).first
        row.locator(".btn-kebab").click()
        expect(_open_menu(page)).to_have_count(1)
        page.keyboard.press("Escape")
        expect(_open_menu(page)).to_have_count(0)

    def test_opening_one_kebab_closes_another(self, page: Page, e2e_server):
        """popover=auto ensures only one kebab is open at a time."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        rows = page.locator("tr", has=page.locator(".badge-builtin"))
        # Need at least two built-in rows (pi-4, pi-5, pi-zero-2w are seeded).
        assert rows.count() >= 2

        rows.nth(0).locator(".btn-kebab").click()
        expect(_open_menu(page)).to_have_count(1)

        # Even with another popover already open, clicking r1's kebab
        # flips to that one (popover=auto auto-closes siblings).
        rows.nth(1).locator(".btn-kebab").dispatch_event("click")
        expect(_open_menu(page)).to_have_count(1)
        expect(rows.nth(1).locator(".btn-kebab")).to_have_attribute("aria-expanded", "true")
        expect(rows.nth(0).locator(".btn-kebab")).to_have_attribute("aria-expanded", "false")

    def test_destructive_items_have_danger_class(self, page: Page, api, e2e_server):
        """Delete / destructive menu items carry the kebab-item-danger class.

        Uses a custom (non-built-in) profile, which shows a Delete item.
        """
        resp = api.post("/api/profiles", json={
            "name": "kebab-danger-test",
            "video_codec": "h264",
        })
        assert resp.status_code == 201, resp.text

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="kebab-danger-test").first
        row.locator(".btn-kebab").click()
        delete_item = _open_menu(page).get_by_role("menuitem", name="Delete")
        expect(delete_item).to_have_class("kebab-item-danger")


@pytest.mark.e2e
class TestKebabPresenceAcrossPages:
    """Every converted table/card should render the shared kebab component."""

    def test_profiles_page_has_kebab(self, page: Page, e2e_server):
        """Built-in profiles table should render a kebab for each row."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")
        expect(page.locator("table .btn-kebab").first).to_be_visible()

    def test_users_page_has_kebab(self, page: Page, e2e_server):
        """Users table should render a kebab per user row."""
        page.goto("/users")
        page.wait_for_load_state("domcontentloaded")
        expect(page.locator("table .btn-kebab").first).to_be_visible()
