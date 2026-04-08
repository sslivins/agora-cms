"""E2E tests for built-in profile protection and copy button.

Verifies that:
- Built-in profiles do NOT show Edit or Delete buttons.
- Built-in profiles DO show a Copy button.
- Non-built-in profiles show Edit, Copy, and Delete buttons.
- Clicking Copy creates a new profile row with 'Copy of' prefix.
"""

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
class TestBuiltinProfileUI:
    """Built-in profiles should hide Edit/Delete and show Copy."""

    def test_builtin_has_no_edit_button(self, page: Page, e2e_server):
        """Built-in profile row should not have an Edit button."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="pi-zero-2w")
        expect(row).to_be_visible()
        expect(row.locator("button", has_text="Edit")).to_have_count(0)

    def test_builtin_has_no_delete_button(self, page: Page, e2e_server):
        """Built-in profile row should not have a Delete button."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="pi-zero-2w")
        expect(row.locator("button", has_text="Delete")).to_have_count(0)

    def test_builtin_has_copy_button(self, page: Page, e2e_server):
        """Built-in profile row should have a Copy button."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="pi-zero-2w")
        expect(row.locator("button", has_text="Copy")).to_be_visible()

    def test_custom_profile_has_all_buttons(self, page: Page, api, e2e_server):
        """Non-built-in profile should show Edit, Copy, and Delete buttons."""
        resp = api.post("/api/profiles", json={
            "name": "e2e-custom-btns",
            "video_codec": "h264",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="e2e-custom-btns")
        expect(row.locator("button", has_text="Edit")).to_be_visible()
        expect(row.locator("button", has_text="Copy")).to_be_visible()
        expect(row.locator("button", has_text="Delete")).to_be_visible()


@pytest.mark.e2e
class TestCopyProfileUI:
    """Copy button should duplicate a profile in the UI."""

    def test_copy_creates_profile_row(self, page: Page, api, e2e_server):
        """Clicking Copy should create a new 'Copy of ...' profile row."""
        resp = api.post("/api/profiles", json={
            "name": "e2e-to-copy",
            "video_codec": "h264",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="e2e-to-copy").first
        row.locator("button", has_text="Copy").click()

        # Wait for page reload after copy
        page.wait_for_load_state("domcontentloaded")

        # The copied profile should appear
        copy_row = page.locator("tr", has_text="Copy of e2e-to-copy")
        expect(copy_row).to_be_visible()

        # The copy should NOT be built-in (should have Edit and Delete)
        expect(copy_row.locator("button", has_text="Edit")).to_be_visible()
        expect(copy_row.locator("button", has_text="Delete")).to_be_visible()

    def test_copy_builtin_creates_editable_profile(self, page: Page, e2e_server):
        """Copying the built-in profile should create an editable copy."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        builtin_row = page.locator("tr", has_text="pi-zero-2w").first
        builtin_row.locator("button", has_text="Copy").click()

        page.wait_for_load_state("domcontentloaded")

        copy_row = page.locator("tr", has_text="Copy of pi-zero-2w")
        expect(copy_row).to_be_visible()
        # The copy should be editable (has Edit button)
        expect(copy_row.locator("button", has_text="Edit")).to_be_visible()
        # Should NOT have built-in badge
        expect(copy_row.locator(".badge-builtin")).to_have_count(0)
