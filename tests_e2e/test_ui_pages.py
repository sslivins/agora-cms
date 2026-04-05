"""Playwright tests for all major UI pages — JS error detection.

Every page in the CMS must load without JavaScript errors. This catches
syntax errors, undefined references, and runtime exceptions that would
silently break interactive features like buttons and modals.
"""

import pytest
from playwright.sync_api import Page, expect


PAGES = [
    ("/", "Dashboard"),
    ("/devices", "Devices"),
    ("/assets", "Assets"),
    ("/schedules", "Schedules"),
    ("/profiles", "Playback Profiles"),
    ("/settings", "Settings"),
]


class TestPageLoads:
    """Every page must load and render without JS errors."""

    @pytest.mark.parametrize("path,title", PAGES)
    def test_page_loads_without_js_errors(self, page: Page, path, title):
        js_errors = []
        page.on("pageerror", lambda err: js_errors.append(str(err)))

        page.goto(path)
        page.wait_for_load_state("domcontentloaded")

        # Page should have loaded — check for any heading (h1 or h2)
        heading = page.locator("h1, h2").first
        expect(heading).to_be_visible(timeout=5000)

        assert not js_errors, f"JS errors on {path}: {js_errors}"

    @pytest.mark.parametrize("path,title", PAGES)
    def test_page_has_navigation(self, page: Page, path, title):
        """Every page should have the nav bar with all tabs."""
        page.goto(path)
        page.wait_for_load_state("domcontentloaded")

        for nav_text in ["Dashboard", "Devices", "Assets", "Schedules", "History"]:
            expect(page.locator(f"nav >> text={nav_text}")).to_be_visible()
        # Settings and Logout are now header icons
        expect(page.locator("a.header-icon[href='/settings']")).to_be_visible()
        expect(page.locator("a.header-icon[href='/logout']")).to_be_visible()

    @pytest.mark.parametrize("path,title", PAGES)
    def test_page_no_console_errors(self, page: Page, path, title):
        """Check for console.error calls (network failures, etc.)."""
        errors = []
        page.on("console", lambda msg: errors.append(msg.text) if msg.type == "error" else None)

        page.goto(path)
        page.wait_for_load_state("networkidle")

        # Filter out known benign errors (e.g., favicon 404)
        real_errors = [e for e in errors if "favicon" not in e.lower()]
        assert not real_errors, f"Console errors on {path}: {real_errors}"
