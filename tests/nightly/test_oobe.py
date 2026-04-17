"""Phase 2: OOBE wizard end-to-end (#250).

Walks the first-run setup wizard with Playwright against the real compose
stack, and verifies SMTP plumbing by sending a test email through Mailpit
and reading it back from the Mailpit API.

Steps exercised:

1. Login with the bootstrap admin (creds pinned in nightly compose overlay).
2. Step 1 — personalize account (new display name, email, password).
3. Step 2 — configure SMTP pointed at the mailpit container, click "Send
   Test", assert Mailpit received the test email.
4. Step 3 — pick a non-default timezone.
5. Step 4 — toggle MCP, finish setup.
6. Verify dashboard renders.
7. Re-login with the *new* admin password to confirm the account-step write
   actually landed in the DB.
"""

from __future__ import annotations

import re

from playwright.sync_api import Page, expect

from tests.nightly.helpers.mailpit import MailpitClient


# Test data — kept local to the module so other tests can pick their own.
NEW_ADMIN_NAME = "Nightly Admin"
NEW_ADMIN_EMAIL = "nightly-admin@agora.test"
NEW_ADMIN_PASSWORD = "nightly-newpass-123"
SMTP_TEST_RECIPIENT = "smtp-probe@agora.test"


def _login(page: Page, username: str, password: str) -> None:
    page.goto("/login")
    page.fill('input[name="email"]', username)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')


def _login_and_open_wizard(page: Page, admin_credentials: tuple[str, str]) -> None:
    user, pw = admin_credentials
    _login(page, user, pw)
    page.wait_for_url(re.compile(r".*/setup.*"), timeout=15_000)
    expect(page.locator("#step-1")).to_be_visible()


def test_oobe_full_walkthrough(
    page: Page,
    mailpit: MailpitClient,
    admin_credentials: tuple[str, str],
) -> None:
    """Walk the full setup wizard end-to-end with a real SMTP round-trip."""
    js_errors: list[str] = []
    page.on("pageerror", lambda err: js_errors.append(str(err)))

    _login_and_open_wizard(page, admin_credentials)

    # ── Step 1 — Personalize account ──────────────────────────────────────
    step1 = page.locator("#step-1")
    page.fill("#admin-name", NEW_ADMIN_NAME)
    page.fill("#admin-email", NEW_ADMIN_EMAIL)
    page.fill("#admin-password", NEW_ADMIN_PASSWORD)
    page.fill("#admin-confirm", NEW_ADMIN_PASSWORD)
    step1.locator("button", has_text="Next").click()

    # ── Step 2 — SMTP, with a real round-trip through Mailpit ─────────────
    step2 = page.locator("#step-2")
    expect(step2).to_be_visible(timeout=10_000)

    page.fill("#smtp-host", "mailpit")
    page.fill("#smtp-port", "1025")
    page.fill("#smtp-from", "cms@agora.test")
    # Mailpit is configured with MP_SMTP_AUTH_ACCEPT_ANY=1, so any
    # username/password is accepted. Leave them populated to exercise the
    # full code path that includes auth.
    page.fill("#smtp-username", "cms")
    page.fill("#smtp-password", "ignored-by-mailpit")

    page.fill("#smtp-test-email", SMTP_TEST_RECIPIENT)
    page.locator("button", has_text="Send Test").click()

    msg = mailpit.wait_for_email(
        to=SMTP_TEST_RECIPIENT,
        subject_contains="SMTP Test",
        timeout=15.0,
    )
    assert msg.from_addr.lower() == "cms@agora.test"
    assert SMTP_TEST_RECIPIENT in [t.lower() for t in msg.to]
    body = (msg.text or "") + (msg.html or "")
    assert "Agora" in body, f"unexpected body: {body[:200]!r}"

    step2.locator("button", has_text="Next").click()

    # ── Step 3 — Timezone (pick a non-default to confirm select works) ────
    step3 = page.locator("#step-3")
    expect(step3).to_be_visible(timeout=10_000)
    page.select_option("#setup-timezone", "America/New_York")
    step3.locator("button", has_text="Next").click()

    # ── Step 4 — MCP toggle + finish ──────────────────────────────────────
    step4 = page.locator("#step-4")
    expect(step4).to_be_visible(timeout=10_000)
    page.check("#setup-mcp-enabled")
    step4.locator("button", has_text="Finish Setup").click()

    # ── Dashboard ─────────────────────────────────────────────────────────
    page.wait_for_url(re.compile(r".*/$|.*/dashboard.*"), timeout=10_000)
    expect(page.locator("h1")).to_contain_text("Dashboard", timeout=10_000)

    assert not js_errors, f"JS errors during wizard: {js_errors}"


def test_oobe_persists_new_admin_password(
    page: Page,
    admin_credentials: tuple[str, str],
    cms_base_url: str,
) -> None:
    """After the first test ran, the account-step password should be live.

    Depends on `test_oobe_full_walkthrough` having executed first in this
    session — they share the same compose stack (session-scoped fixture).
    Bootstrap creds are no longer accepted; the new email + password are.
    """
    # Old bootstrap creds must NOT log in any more.
    _login(page, admin_credentials[0], admin_credentials[1])
    # CMS treats bad creds by re-rendering /login (status 200) with an
    # error banner — easier to assert by URL.
    page.wait_for_load_state("domcontentloaded")
    assert "/login" in page.url, (
        f"Bootstrap admin creds still work after wizard; landed on {page.url}"
    )

    # New creds (email + password set in step 1) should work.
    _login(page, NEW_ADMIN_EMAIL, NEW_ADMIN_PASSWORD)
    page.wait_for_url(re.compile(r".*/$|.*/dashboard.*"), timeout=10_000)
    expect(page.locator("h1")).to_contain_text("Dashboard", timeout=10_000)


def test_oobe_setup_route_redirects_after_completion(
    page: Page,
) -> None:
    """Visiting /setup after the wizard finished should bounce to /."""
    # Anonymous request — should redirect to /login (which is the standard
    # behaviour) rather than re-rendering the wizard.
    response = page.goto("/setup")
    assert response is not None
    # After completion the middleware no longer forces /setup; either we
    # land on /login (unauth) or / (if we still have a cookie). Both are
    # acceptable; what we care about is that we did NOT re-render step-1.
    assert "Welcome to Agora CMS" not in page.content()
