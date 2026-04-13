"""Playwright E2E tests for the schedule history UI."""

import pytest
from playwright.sync_api import Page, expect


class TestHistoryTab:
    """Test the History tab navigation and page rendering."""

    def test_history_tab_in_nav(self, page: Page):
        """The nav bar contains a History tab."""
        page.goto("/")
        nav = page.locator("nav a, .nav a, .tabs a")
        texts = nav.all_text_contents()
        assert any("History" in t for t in texts), f"No 'History' tab found in nav: {texts}"

    def test_history_page_loads(self, page: Page):
        """GET /history returns 200 and renders the history heading."""
        resp = page.goto("/history")
        assert resp.status is not None and resp.status == 200
        expect(page.locator("h1, h2").first).to_contain_text("History", timeout=5000)

    def test_history_page_has_table_or_empty_state(self, page: Page):
        """History page shows either a log table or an empty-state message."""
        page.goto("/history")
        table = page.locator("table")
        empty = page.locator(".empty-state")
        assert table.count() > 0 or empty.count() > 0, "Neither table nor empty-state found"

    def test_history_page_no_js_errors(self, page: Page):
        """No uncaught JS errors on the history page."""
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto("/history")
        page.wait_for_load_state("networkidle")
        assert errors == [], f"JS errors on history page: {errors}"


class TestDashboardRecentActivity:
    """Test the Recent Activity panel on the dashboard."""

    def test_recent_activity_panel_exists(self, page: Page):
        """Dashboard has a Recent Activity section."""
        page.goto("/")
        page.wait_for_load_state("networkidle")
        # Look for the heading or panel containing "Recent Activity"
        body_text = page.text_content("body")
        assert "Recent Activity" in body_text or "recent activity" in body_text.lower()

    def test_recent_activity_view_full_link(self, page: Page):
        """Dashboard has a link to the full history page."""
        page.goto("/")
        page.wait_for_load_state("networkidle")
        link = page.locator("a[href='/history'], a[href*='history']")
        expect(link.first).to_be_visible(timeout=5000)

    def test_click_history_link_navigates(self, page: Page):
        """Clicking the history link navigates to /history."""
        page.goto("/")
        page.wait_for_load_state("networkidle")
        link = page.locator("a[href='/history'], a[href*='history']").first
        link.click()
        page.wait_for_url("**/history")

    def test_history_event_badges_render(self, page: Page, api, ws_url, e2e_server):
        """Event badges have proper CSS classes after seeding a log entry."""
        from tests_e2e.fake_device import FakeDevice
        from tests_e2e.conftest import run_async

        # Register device via WebSocket and adopt it
        async def register():
            async with FakeDevice("e2e-hist-pi", ws_url) as dev:
                await dev.send_status()

        run_async(register())
        api.post("/api/devices/e2e-hist-pi/adopt")

        upload = api.create_asset(filename="e2e-history.mp4")
        asset_id = upload.json()["id"]

        sched = api.post("/api/schedules", json={
            "name": "E2E History Schedule",
            "device_id": "e2e-hist-pi",
            "asset_id": asset_id,
            "start_time": "00:00",
            "end_time": "23:59",
        })
        sched_id = sched.json()["id"]

        # End the schedule now — this logs a SKIPPED event
        api.post(f"/api/schedules/{sched_id}/end-now")

        # Visit history page and check for badge
        page.goto("/history")
        page.wait_for_load_state("networkidle")
        badge = page.locator(".badge-skipped")
        expect(badge.first).to_be_visible(timeout=5000)

    def test_dashboard_no_js_errors(self, page: Page):
        """No uncaught JS errors on the dashboard."""
        errors = []
        page.on("pageerror", lambda err: errors.append(str(err)))
        page.goto("/")
        page.wait_for_load_state("networkidle")
        assert errors == [], f"JS errors on dashboard: {errors}"
