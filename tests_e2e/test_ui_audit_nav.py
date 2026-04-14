"""Test that the audit log page renders user session state correctly.

Regression test: navigating to /audit caused the header to lose the
user's display name, group badges, and permission-gated tabs because
require_permission() did not set request.state.user.
"""

import re

import pytest
from playwright.sync_api import Page, expect


class TestAuditPageNavigation:
    """The audit log page must render the same nav chrome as every other page."""

    def test_audit_page_shows_username(self, page: Page):
        """User's display name must appear in the header on /audit."""
        page.goto("/audit")
        page.wait_for_load_state("domcontentloaded")

        greeting = page.locator(".header-greeting")
        expect(greeting).to_be_visible(timeout=5000)
        expect(greeting).not_to_be_empty()

    def test_audit_page_shows_nav_tabs(self, page: Page):
        """All standard nav tabs must be visible on /audit."""
        page.goto("/audit")
        page.wait_for_load_state("domcontentloaded")

        for tab_text in ["Dashboard", "Devices", "Assets", "Schedules", "History"]:
            expect(page.locator(f"nav >> text={tab_text}")).to_be_visible()

    def test_audit_page_shows_audit_tab(self, page: Page):
        """The Audit Log tab itself must be visible (admin has audit:read)."""
        page.goto("/audit")
        page.wait_for_load_state("domcontentloaded")

        expect(page.locator("nav >> text=Audit Log")).to_be_visible()

    def test_audit_page_shows_users_tab(self, page: Page):
        """The Users tab must be visible for admin on /audit."""
        page.goto("/audit")
        page.wait_for_load_state("domcontentloaded")

        expect(page.locator("nav >> text=Users")).to_be_visible()

    def test_audit_page_hides_group_badge_for_admin(self, page: Page):
        """The group badge must be hidden for admin (has groups:view_all)."""
        page.goto("/audit")
        page.wait_for_load_state("domcontentloaded")

        group_badge = page.locator(".header-groups")
        expect(group_badge).to_be_hidden(timeout=5000)

    def test_audit_tab_active(self, page: Page):
        """The Audit Log tab should be the active tab on /audit."""
        page.goto("/audit")
        page.wait_for_load_state("domcontentloaded")

        audit_tab = page.locator("nav >> text=Audit Log")
        expect(audit_tab).to_have_class(re.compile(r"active"))
