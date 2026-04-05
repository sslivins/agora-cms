"""Playwright tests for checksum copy buttons on the Assets page."""

import pytest
from playwright.sync_api import Page, expect


class TestCopyChecksumButton:
    """Copy checksum buttons should work on the assets page."""

    def _upload_asset(self, api):
        """Upload a test asset and return its data."""
        resp = api.create_asset(filename="copy-test.mp4", content=b"fake-mp4")
        assert resp.status_code == 201
        return resp.json()

    def test_asset_checksum_copy_button_exists(self, page: Page, api):
        """The Copy button should appear next to the asset checksum."""
        asset = self._upload_asset(api)

        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")

        # Expand the asset detail row
        row = page.locator("tr", has_text="copy-test.mp4").first
        expect(row).to_be_visible(timeout=5000)
        row.locator(".expand-toggle").click()

        # The copy button should be visible in the detail area
        copy_btn = page.locator(".btn-copy").first
        expect(copy_btn).to_be_visible(timeout=3000)
        expect(copy_btn).to_have_text("Copy")

    def test_asset_checksum_copy_shows_checkmark(self, page: Page, api, context):
        """Clicking Copy should change the button text to a checkmark."""
        asset = self._upload_asset(api)

        # Grant clipboard permissions for the test context
        context.grant_permissions(["clipboard-read", "clipboard-write"])

        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")

        # Expand the asset detail row
        row = page.locator("tr", has_text="copy-test.mp4").first
        expect(row).to_be_visible(timeout=5000)
        row.locator(".expand-toggle").click()

        # Find and click the Copy button
        copy_btn = page.locator(".btn-copy").first
        expect(copy_btn).to_be_visible(timeout=3000)
        copy_btn.click()

        # Button should change to checkmark
        expect(copy_btn).to_have_text("\u2713", timeout=3000)

    def test_asset_checksum_copy_reverts_text(self, page: Page, api, context):
        """After showing the checkmark, the button should revert to 'Copy'."""
        asset = self._upload_asset(api)

        context.grant_permissions(["clipboard-read", "clipboard-write"])

        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="copy-test.mp4").first
        expect(row).to_be_visible(timeout=5000)
        row.locator(".expand-toggle").click()

        copy_btn = page.locator(".btn-copy").first
        expect(copy_btn).to_be_visible(timeout=3000)
        copy_btn.click()

        expect(copy_btn).to_have_text("\u2713", timeout=3000)
        # Should revert back to "Copy" after ~1.5 seconds
        expect(copy_btn).to_have_text("Copy", timeout=5000)

    def test_checksum_copy_writes_correct_value(self, page: Page, api, context):
        """The copied text should match the asset's checksum."""
        asset = self._upload_asset(api)
        expected_checksum = asset["checksum"]

        context.grant_permissions(["clipboard-read", "clipboard-write"])

        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="copy-test.mp4").first
        expect(row).to_be_visible(timeout=5000)
        row.locator(".expand-toggle").click()

        copy_btn = page.locator(".btn-copy").first
        expect(copy_btn).to_be_visible(timeout=3000)
        copy_btn.click()

        # Verify the clipboard contains the correct checksum
        clipboard = page.evaluate("() => navigator.clipboard.readText()")
        assert clipboard == expected_checksum
