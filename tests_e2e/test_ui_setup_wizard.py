"""Playwright end-to-end tests for the first-run setup wizard."""

from playwright.sync_api import Page, expect


class TestSetupWizardRedirects:
    """Verify middleware redirects when setup is incomplete."""

    def test_root_redirects_to_setup(
        self, browser_instance, base_url, setup_incomplete,
    ):
        """Unauthenticated visit to / should redirect to /setup then /login."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        page.goto("/")
        page.wait_for_url("**/login*")

        page.close()
        ctx.close()

    def test_setup_redirects_to_login(
        self, browser_instance, base_url, setup_incomplete,
    ):
        """GET /setup without auth should redirect to /login."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        page.goto("/setup")
        page.wait_for_url("**/login*")

        page.close()
        ctx.close()


class TestSetupWizardFlow:
    """Full happy-path flow through the setup wizard."""

    def test_login_then_setup_renders(
        self, browser_instance, base_url, setup_incomplete,
    ):
        """After login, user should land on the setup wizard."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        # Login with default credentials
        page.goto("/login")
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "testpass")
        page.click('button[type="submit"]')

        # Should end up on /setup (middleware redirects / → /setup)
        page.wait_for_url("**/setup*")
        expect(page.locator("text=Welcome to Agora CMS")).to_be_visible()

        page.close()
        ctx.close()

    def test_full_wizard_completion(
        self, browser_instance, base_url, setup_incomplete,
    ):
        """Complete all wizard steps and verify redirect to dashboard."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        js_errors = []
        page.on("pageerror", lambda err: js_errors.append(str(err)))

        # Login
        page.goto("/login")
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "testpass")
        page.click('button[type="submit"]')
        page.wait_for_url("**/setup*")

        # Step 1: Personalize account
        step1 = page.locator("#step-1")
        expect(step1).to_be_visible()
        page.fill("#admin-name", "E2E Admin")
        page.fill("#admin-email", "e2e@example.com")
        page.fill("#admin-password", "testpass")
        page.fill("#admin-confirm", "testpass")
        step1.locator("button", has_text="Next").click()

        # Step 2: Skip SMTP
        step2 = page.locator("#step-2")
        expect(step2).to_be_visible(timeout=5000)
        step2.locator("button", has_text="Skip").click()

        # Step 3: Timezone — just accept default
        step3 = page.locator("#step-3")
        expect(step3).to_be_visible(timeout=5000)
        step3.locator("button", has_text="Next").click()

        # Step 4: MCP — finish
        step4 = page.locator("#step-4")
        expect(step4).to_be_visible(timeout=5000)
        step4.locator("button", has_text="Finish Setup").click()

        # Should redirect to dashboard
        page.wait_for_url("**/", timeout=5000)
        expect(page.locator("h1")).to_contain_text("Dashboard", timeout=5000)

        assert not js_errors, f"JS errors during setup: {js_errors}"

        page.close()
        ctx.close()

    def test_validation_bad_email(
        self, browser_instance, base_url, setup_incomplete,
    ):
        """Account step should reject an invalid email."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        # Login
        page.goto("/login")
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "testpass")
        page.click('button[type="submit"]')
        page.wait_for_url("**/setup*")

        # Step 1: Bad email
        page.fill("#admin-name", "Test")
        page.fill("#admin-email", "not-an-email")
        page.fill("#admin-password", "testpass")
        page.fill("#admin-confirm", "testpass")
        page.locator("#step-1 button", has_text="Next").click()

        # Should show error and stay on step 1
        expect(page.locator("text=valid email")).to_be_visible(timeout=3000)

        page.close()
        ctx.close()

    def test_validation_short_password(
        self, browser_instance, base_url, setup_incomplete,
    ):
        """Account step should reject a password shorter than 6 characters."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        # Login
        page.goto("/login")
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "testpass")
        page.click('button[type="submit"]')
        page.wait_for_url("**/setup*")

        # Step 1: Short password
        page.fill("#admin-name", "Test")
        page.fill("#admin-email", "test@example.com")
        page.fill("#admin-password", "abc")
        page.fill("#admin-confirm", "abc")
        page.locator("#step-1 button", has_text="Next").click()

        # Should show error
        expect(page.locator("text=6 characters")).to_be_visible(timeout=3000)

        page.close()
        ctx.close()

    def test_validation_password_mismatch(
        self, browser_instance, base_url, setup_incomplete,
    ):
        """Account step should reject mismatched passwords (client-side)."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        # Login
        page.goto("/login")
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "testpass")
        page.click('button[type="submit"]')
        page.wait_for_url("**/setup*")

        # Step 1: Mismatched passwords
        page.fill("#admin-name", "Test")
        page.fill("#admin-email", "test@example.com")
        page.fill("#admin-password", "password1")
        page.fill("#admin-confirm", "password2")
        page.locator("#step-1 button", has_text="Next").click()

        # Should show client-side error
        expect(page.locator("text=match")).to_be_visible(timeout=3000)

        page.close()
        ctx.close()


class TestSetupWizardReentry:
    """Verify setup wizard cannot be re-entered after completion."""

    def test_setup_redirects_after_completion(
        self, browser_instance, base_url, e2e_server,
    ):
        """After setup is complete, /setup should redirect to dashboard."""
        ctx = browser_instance.new_context(base_url=base_url)
        page = ctx.new_page()

        # Login (setup already completed by conftest)
        page.goto("/login")
        page.fill('input[name="email"]', "admin")
        page.fill('input[name="password"]', "testpass")
        page.click('button[type="submit"]')
        page.wait_for_url("**/")

        # Try to access setup directly
        page.goto("/setup")
        page.wait_for_url("**/", timeout=3000)

        # Should be on dashboard, not setup
        expect(page.locator("h1")).to_contain_text("Dashboard")

        page.close()
        ctx.close()
