"""Playwright tests for login and authentication."""

from playwright.sync_api import Page, expect


class TestLogin:
    """Login page and authentication flow."""

    def test_login_page_loads(self, browser_instance, base_url, e2e_server):
        """Login page should render without errors."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        js_errors = []
        page.on("pageerror", lambda err: js_errors.append(str(err)))

        page.goto("/login")
        expect(page.locator("button[type='submit']")).to_be_visible()
        assert not js_errors

        page.close()
        ctx.close()

    def test_login_with_valid_credentials(self, browser_instance, base_url, e2e_server):
        """Valid credentials should redirect to dashboard."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        page.goto("/login")
        page.fill('input[name="username"]', "admin")
        page.fill('input[name="password"]', "testpass")
        page.click('button[type="submit"]')

        # Should redirect to dashboard
        page.wait_for_url("**/")
        expect(page.locator("h1")).to_contain_text("Dashboard")

        page.close()
        ctx.close()

    def test_login_with_bad_credentials(self, browser_instance, base_url, e2e_server):
        """Invalid credentials should show error on login page."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        page.goto("/login")
        page.fill('input[name="username"]', "admin")
        page.fill('input[name="password"]', "wrongpassword")
        page.click('button[type="submit"]')

        # Should stay on login page with error
        expect(page.locator("text=Invalid")).to_be_visible(timeout=3000)

        page.close()
        ctx.close()

    def test_unauthenticated_redirect(self, browser_instance, base_url, e2e_server):
        """Unauthenticated access to protected pages should redirect to login."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        page.goto("/schedules")
        page.wait_for_url("**/login*")

        page.close()
        ctx.close()
