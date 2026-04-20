"""E2E tests for built-in profile UI: edit, reset, copy, and delete.

Verifies that:
- Built-in profiles DO show Edit, Copy, and Reset buttons.
- Built-in profiles do NOT show Delete buttons.
- Non-built-in profiles show Edit, Copy, and Delete buttons (no Reset).
- Clicking Copy creates a new profile row with 'Copy of' prefix.
"""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import click_row_action


@pytest.mark.e2e
class TestBuiltinProfileUI:
    """Built-in profiles should show Edit/Copy/Reset but hide Delete."""

    def test_builtin_has_edit_button(self, page: Page, e2e_server):
        """Built-in profile rows should have Edit buttons."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        rows = page.locator("tr", has=page.locator(".badge-builtin"))
        expect(rows.first).to_be_visible()
        for i in range(rows.count()):
            expect(rows.nth(i).locator('[role="menuitem"]', has_text="Edit")).to_have_count(1)

    def test_builtin_has_reset_button(self, page: Page, e2e_server):
        """Built-in profile rows should have Reset buttons."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        rows = page.locator("tr", has=page.locator(".badge-builtin"))
        for i in range(rows.count()):
            expect(rows.nth(i).locator('[role="menuitem"]', has_text="Reset")).to_have_count(1)

    def test_builtin_has_no_delete_button(self, page: Page, e2e_server):
        """Built-in profile rows should not have Delete buttons."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        rows = page.locator("tr", has=page.locator(".badge-builtin"))
        for i in range(rows.count()):
            expect(rows.nth(i).locator('[role="menuitem"]', has_text="Delete")).to_have_count(0)

    def test_builtin_has_copy_button(self, page: Page, e2e_server):
        """Built-in profile rows should have Copy buttons."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        rows = page.locator("tr", has=page.locator(".badge-builtin"))
        for i in range(rows.count()):
            expect(rows.nth(i).locator('[role="menuitem"]', has_text="Copy")).to_have_count(1)

    def test_custom_profile_has_all_buttons(self, page: Page, api, e2e_server):
        """Non-built-in profile should show Edit, Copy, and Delete buttons (no Reset)."""
        resp = api.post("/api/profiles", json={
            "name": "e2e-custom-btns",
            "video_codec": "h264",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="e2e-custom-btns")
        expect(row.locator('[role="menuitem"]', has_text="Edit")).to_have_count(1)
        expect(row.locator('[role="menuitem"]', has_text="Copy")).to_have_count(1)
        expect(row.locator('[role="menuitem"]', has_text="Delete")).to_have_count(1)
        expect(row.locator('[role="menuitem"]', has_text="Reset")).to_have_count(0)


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
        click_row_action(row, "Copy")

        # Wait for page reload after copy
        page.wait_for_load_state("domcontentloaded")

        # The copied profile should appear
        copy_row = page.locator("tr", has_text="Copy of e2e-to-copy").first
        expect(copy_row).to_be_visible()

        # The copy should NOT be built-in (should have Edit and Delete)
        expect(copy_row.locator('[role="menuitem"]', has_text="Edit")).to_have_count(1)
        expect(copy_row.locator('[role="menuitem"]', has_text="Delete")).to_have_count(1)

    def test_copy_builtin_creates_editable_profile(self, page: Page, e2e_server):
        """Copying a built-in profile should create an editable copy."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        builtin_row = page.locator("tr", has=page.locator(".badge-builtin")).first
        click_row_action(builtin_row, "Copy")

        page.wait_for_load_state("domcontentloaded")

        # The first built-in profile is pi-4; its copy should appear
        copy_row = page.locator("tr", has_text="Copy of pi-4").first
        expect(copy_row).to_be_visible()
        # The copy should be editable (has Edit button)
        expect(copy_row.locator('[role="menuitem"]', has_text="Edit")).to_have_count(1)
        # Should NOT have built-in badge
        expect(copy_row.locator(".badge-builtin")).to_have_count(0)
