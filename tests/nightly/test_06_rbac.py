"""Phase 7: RBAC / user profiles (#250).

Exercises the user-management + permission-scoping flows against the real
compose stack:

1. Admin seeds two device groups (A and B) and reads the built-in role IDs.
2. Admin creates three users via ``POST /api/users``:
     - Operator A  → Group A
     - Viewer      → Group A
     - Operator B  → Group B
3. For each new user we walk the real welcome-email flow:
     a. Mailpit receives the welcome email.
     b. Follow ``/setup-account?token=...`` in a fresh browser context
        (invalidates the token, sets a session cookie, redirects to
        ``/force-password-change``).
     c. Submit the new password via the force-password-change form.
     d. Log in via ``/login`` with the new creds.
4. Assert the permission matrix:
     - Operator A can CRUD resources in Group A.
     - Operator A gets 403/404 touching Group B.
     - Viewer gets 200 on reads, 403 on writes.
     - Operator B cannot see Operator A's schedule.

Depends on Phase 2 (OOBE) having configured SMTP to point at Mailpit, and
on Phase 3 (assets) + Phase 4 (devices) having populated the DB with
transcoded assets + adopted devices. Each test asserts only what's needed
for its own invariant — module ordering (via alphabetical filenames and
alphabetical test names) carries shared state in module-level dicts.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any

import pytest
from playwright.sync_api import BrowserContext, Page

from tests.nightly.helpers.mailpit import MailpitClient


# ── identities ─────────────────────────────────────────────────────────────

GROUP_A_NAME = "Nightly RBAC Group A"
GROUP_B_NAME = "Nightly RBAC Group B"

OPERATOR_A = {
    "email": "operator-a@agora.test",
    "display_name": "Operator A",
    "password": "op-a-newpass-123",
}
OPERATOR_B = {
    "email": "operator-b@agora.test",
    "display_name": "Operator B",
    "password": "op-b-newpass-123",
}
VIEWER = {
    "email": "viewer@agora.test",
    "display_name": "Viewer",
    "password": "viewer-newpass-123",
}

# Shared across tests in the module. Populated by earlier tests, read by later
# ones. Keys documented here so mutating tests can see what they need to fill.
STATE: dict[str, Any] = {
    # filled by test_01
    "role_ids": {},          # {"Admin": uuid, "Operator": uuid, "Viewer": uuid}
    "group_a_id": None,
    "group_b_id": None,
    # filled by test_05
    "schedule_a_id": None,
}


# ── tiny HTTP helpers (same shape as earlier phases) ──────────────────────


def _api_get(page: Page, path: str, *, expected: int = 200) -> Any:
    resp = page.request.get(path)
    assert resp.status == expected, f"GET {path} -> {resp.status}: {resp.text()[:400]}"
    return resp.json() if resp.text() else {}


def _api_post(page: Page, path: str, body: dict, *, expected: int = 201) -> Any:
    resp = page.request.post(path, data=body)
    assert resp.status == expected, (
        f"POST {path} -> {resp.status} (expected {expected}): {resp.text()[:400]}"
    )
    return resp.json() if resp.text() else {}


def _api_patch(page: Page, path: str, body: dict, *, expected: int = 200) -> Any:
    resp = page.request.patch(path, data=body)
    assert resp.status == expected, (
        f"PATCH {path} -> {resp.status} (expected {expected}): {resp.text()[:400]}"
    )
    return resp.json() if resp.text() else {}


def _status(page: Page, method: str, path: str, body: dict | None = None) -> int:
    """Return status code only — used for permission-denial assertions."""
    kwargs = {"data": body} if body is not None else {}
    resp = getattr(page.request, method.lower())(path, **kwargs)
    return resp.status


# ── user-creation helpers ─────────────────────────────────────────────────


def _pick_role_id(admin_page: Page, role_name: str) -> str:
    """Return the built-in role ID for the given name."""
    roles = _api_get(admin_page, "/api/roles")
    for r in roles:
        if r["name"] == role_name:
            return r["id"]
    raise AssertionError(f"role {role_name!r} not found in {[r['name'] for r in roles]}")


def _create_user(admin_page: Page, *, email: str, display_name: str,
                 role_id: str, group_ids: list[str]) -> dict:
    return _api_post(
        admin_page,
        "/api/users",
        {
            "email": email,
            "display_name": display_name,
            "role_id": role_id,
            "group_ids": group_ids,
        },
    )


def _complete_account_setup(
    browser_context: BrowserContext,
    *,
    cms_base_url: str,
    mailpit: MailpitClient,
    email: str,
    new_password: str,
) -> None:
    """Walk the welcome-email magic-link flow end-to-end.

    After this returns, the user's password is set and ``must_change_password``
    is cleared. The caller is free to open a fresh browser context and log in
    via ``/login`` with ``(email, new_password)``.
    """
    msg = mailpit.wait_for_email(to=email, subject_contains="Welcome", timeout=30.0)
    link = msg.find_link(r"https?://\S*?/setup-account\?token=[\w\-]+")
    assert link, f"no /setup-account link in welcome email to {email}; body={msg.text[:300]!r}"

    # The link embeds the CMS's own base_url, which from inside the compose
    # network is http://cms:8080 — not reachable from the test runner. Rewrite
    # to the host-exposed URL the test harness uses.
    path_and_query = re.sub(r"^https?://[^/]+", "", link)
    setup_url = f"{cms_base_url.rstrip('/')}{path_and_query}"

    page = browser_context.new_page()
    try:
        page.goto(setup_url)
        # /setup-account redirects (303) → /force-password-change. Playwright
        # follows automatically.
        page.wait_for_url(re.compile(r".*/force-password-change/?$"), timeout=10_000)
        page.fill('input[name="new_password"]', new_password)
        page.fill('input[name="confirm_password"]', new_password)
        page.locator('form[action="/force-password-change"] button[type="submit"]').click()
        # On success it 303's to /. Landing page may itself redirect — just
        # make sure we left the force-password-change page.
        page.wait_for_load_state("networkidle", timeout=10_000)
        assert "/force-password-change" not in page.url, (
            f"still on force-password-change after submit; url={page.url}"
        )
    finally:
        page.close()


def _login_page(browser_context: BrowserContext, email: str, password: str) -> Page:
    """Log a fresh page in as (email, password) and return it."""
    page = browser_context.new_page()
    page.goto("/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url(re.compile(r".*/(?!login).*$|.*/$"), timeout=15_000)
    assert "/login" not in page.url, f"login for {email} failed, still on {page.url}"
    return page


# ── tests (alphabetical order is the execution order) ─────────────────────


def test_01_admin_seeds_groups_and_reads_roles(authenticated_page: Page) -> None:
    """Admin creates Group A + B and remembers the built-in role IDs."""
    for name in ("Admin", "Operator", "Viewer"):
        STATE["role_ids"][name] = _pick_role_id(authenticated_page, name)

    grp_a = _api_post(authenticated_page, "/api/devices/groups/", {"name": GROUP_A_NAME})
    grp_b = _api_post(authenticated_page, "/api/devices/groups/", {"name": GROUP_B_NAME})
    STATE["group_a_id"] = grp_a["id"]
    STATE["group_b_id"] = grp_b["id"]
    assert grp_a["id"] != grp_b["id"]


@pytest.mark.parametrize(
    "identity,role_name,group_key",
    [
        (OPERATOR_A, "Operator", "group_a_id"),
        (VIEWER, "Viewer", "group_a_id"),
        (OPERATOR_B, "Operator", "group_b_id"),
    ],
    ids=["operator_a", "viewer", "operator_b"],
)
def test_02_admin_creates_user_and_setup_flow_completes(
    authenticated_page: Page,
    browser_context: BrowserContext,
    mailpit: MailpitClient,
    cms_base_url: str,
    identity: dict,
    role_name: str,
    group_key: str,
) -> None:
    """Admin creates user → mailpit email → magic link → new password → login."""
    mailpit.delete_all()

    role_id = STATE["role_ids"][role_name]
    group_id = STATE[group_key]
    assert role_id and group_id, "prior test (test_01) must have seeded role + group ids"

    created = _create_user(
        authenticated_page,
        email=identity["email"],
        display_name=identity["display_name"],
        role_id=role_id,
        group_ids=[group_id],
    )
    assert created["email"] == identity["email"]
    assert created["role_id"] == role_id
    assert group_id in created["group_ids"]
    assert created.get("must_change_password") is True

    _complete_account_setup(
        browser_context,
        cms_base_url=cms_base_url,
        mailpit=mailpit,
        email=identity["email"],
        new_password=identity["password"],
    )

    # Sanity: log in, hit /api/users/me, confirm role + group + no must-change flag.
    user_page = _login_page(browser_context, identity["email"], identity["password"])
    me = _api_get(user_page, "/api/users/me")
    assert me["email"] == identity["email"]
    assert me["role"]["name"] == role_name
    assert group_id in me["group_ids"]


def test_03_operator_a_can_read_and_write_in_own_group(
    browser_context: BrowserContext,
) -> None:
    """Operator A: sees Group A but not Group B; can PATCH Group A; can create a schedule on Group A."""
    op_page = _login_page(browser_context, OPERATOR_A["email"], OPERATOR_A["password"])

    # Group visibility: A yes, B no (group scoping)
    groups = _api_get(op_page, "/api/devices/groups/")
    visible = {g["id"] for g in groups}
    assert STATE["group_a_id"] in visible, f"operator A can't see their own group; got {visible}"
    assert STATE["group_b_id"] not in visible, (
        f"operator A can see Group B (isolation broken); got {visible}"
    )

    # PATCH own group → 200
    renamed = _api_patch(
        op_page,
        f"/api/devices/groups/{STATE['group_a_id']}",
        {"name": f"{GROUP_A_NAME} (renamed by op-a)"},
    )
    assert renamed["name"].endswith("renamed by op-a)")

    # Create a schedule on Group A using any transcoded video asset (from Phase 3).
    assets = _api_get(op_page, "/api/assets")
    video_assets = [a for a in assets if a.get("asset_type") == "video" and a.get("duration_seconds")]
    assert video_assets, "Phase 3 was supposed to leave at least one transcoded video asset"
    asset = video_assets[0]

    schedule = _api_post(
        op_page,
        "/api/schedules",
        {
            "name": "Operator A nightly schedule",
            "asset_id": asset["id"],
            "group_id": STATE["group_a_id"],
            "start_time": "00:00:00",
            "end_time": "23:59:59",
            "priority": 0,
        },
    )
    STATE["schedule_a_id"] = schedule["id"]
    assert schedule["group_id"] == STATE["group_a_id"]


def test_03a_operator_a_creates_own_group_and_sees_it(
    browser_context: BrowserContext,
    cms_base_url: str,
) -> None:
    """Regression test for the 'Add Group does nothing' bug.

    When a non-admin user creates a group via the /devices UI, the creator
    must be auto-added to the new group's membership — otherwise the
    subsequent list_groups call (which scopes to the user's assigned groups
    for non-admins) hides the group from its own creator, making it look
    like the create button silently failed.

    Exercises the full flow through Playwright: fill the form, click the
    button, wait for the page reload, assert the group card is rendered,
    and double-check via the API that GET /api/devices/groups/ includes it.
    """
    op_page = _login_page(browser_context, OPERATOR_A["email"], OPERATOR_A["password"])
    op_page.goto(f"{cms_base_url.rstrip('/')}/devices")
    op_page.wait_for_load_state("networkidle", timeout=10_000)

    group_name = f"Op A self-created {uuid.uuid4().hex[:8]}"

    # The Add Group form is only rendered for users with groups:write —
    # this assertion doubles as a guard that the template gate is correct.
    name_input = op_page.locator("#group-name")
    add_btn = op_page.locator("#add-group-btn")
    assert name_input.is_visible(), "Add Group form not rendered for Operator"

    name_input.fill(group_name)
    # gateButtonOnInputs enables the button only once the name field has content.
    op_page.wait_for_function(
        "() => !document.querySelector('#add-group-btn').disabled",
        timeout=5_000,
    )
    add_btn.click()
    # createGroup() POSTs then calls location.reload().
    op_page.wait_for_load_state("networkidle", timeout=10_000)

    # API-level assertion using the now-logged-in browser session: the
    # filtered list for this operator must include the group they just
    # created. If the creator was not auto-added to user_groups, the
    # server-side scoping filter would hide it here and the regression
    # would fire. This is the precise contract that broke in prod.
    groups = _api_get(op_page, "/api/devices/groups/")
    names = [g["name"] for g in groups]
    assert group_name in names, (
        f"Operator created group {group_name!r} but it's not in their "
        f"GET /api/devices/groups/ response — the creator was not auto-added. "
        f"Visible groups: {names}"
    )


def test_04_operator_a_cannot_touch_group_b(
    browser_context: BrowserContext,
) -> None:
    """Operator A: PATCH/DELETE on Group B should be denied (403 or 404)."""
    op_page = _login_page(browser_context, OPERATOR_A["email"], OPERATOR_A["password"])

    # PATCH attempt on Group B
    status = _status(
        op_page, "PATCH",
        f"/api/devices/groups/{STATE['group_b_id']}",
        {"name": "should-not-succeed"},
    )
    assert status in (403, 404), f"expected 403/404, got {status}"

    # DELETE attempt on Group B
    status = _status(
        op_page, "DELETE",
        f"/api/devices/groups/{STATE['group_b_id']}",
    )
    assert status in (403, 404), f"expected 403/404, got {status}"


def test_05_viewer_is_read_only(browser_context: BrowserContext) -> None:
    """Viewer: GETs return 200; any POST / PATCH / DELETE returns 403."""
    v_page = _login_page(browser_context, VIEWER["email"], VIEWER["password"])

    # Reads succeed
    _api_get(v_page, "/api/devices/groups/")
    _api_get(v_page, "/api/schedules")
    _api_get(v_page, "/api/assets")

    # Writes denied
    assert _status(
        v_page, "POST", "/api/devices/groups/", {"name": "viewer-cannot-make-groups"}
    ) == 403
    assert _status(
        v_page, "PATCH",
        f"/api/devices/groups/{STATE['group_a_id']}",
        {"name": "viewer-cannot-rename"},
    ) == 403
    # Should not be able to create a schedule even in their own group
    assets = _api_get(v_page, "/api/assets")
    if assets:
        status = _status(
            v_page, "POST", "/api/schedules",
            {
                "name": "viewer-cannot-schedule",
                "asset_id": assets[0]["id"],
                "group_id": STATE["group_a_id"],
                "start_time": "00:00:00",
                "end_time": "23:59:59",
            },
        )
        assert status == 403, f"viewer POST /api/schedules -> {status}, expected 403"


def test_06_operator_b_cannot_see_operator_a_schedule(
    browser_context: BrowserContext,
) -> None:
    """Cross-operator isolation: B doesn't see A's schedule or group."""
    b_page = _login_page(browser_context, OPERATOR_B["email"], OPERATOR_B["password"])

    schedules = _api_get(b_page, "/api/schedules")
    sched_ids = {s["id"] for s in schedules}
    assert STATE["schedule_a_id"] not in sched_ids, (
        f"operator B sees operator A's schedule (isolation broken); got {sched_ids}"
    )

    # Direct fetch of A's schedule → 403/404
    status = _status(b_page, "GET", f"/api/schedules/{STATE['schedule_a_id']}")
    assert status in (403, 404), f"expected 403/404, got {status}"

    # Group A invisible and PATCH denied.
    groups = _api_get(b_page, "/api/devices/groups/")
    assert STATE["group_a_id"] not in {g["id"] for g in groups}

    status = _status(
        b_page, "PATCH",
        f"/api/devices/groups/{STATE['group_a_id']}",
        {"name": "should-not-succeed"},
    )
    assert status in (403, 404)


def test_07_admin_still_sees_everything(authenticated_page: Page) -> None:
    """Sanity: admin sees both groups, the operator-A schedule, and all users."""
    groups = _api_get(authenticated_page, "/api/devices/groups/")
    visible = {g["id"] for g in groups}
    assert STATE["group_a_id"] in visible
    assert STATE["group_b_id"] in visible

    schedules = _api_get(authenticated_page, "/api/schedules")
    assert STATE["schedule_a_id"] in {s["id"] for s in schedules}

    users = _api_get(authenticated_page, "/api/users")
    emails = {u["email"] for u in users}
    assert OPERATOR_A["email"] in emails
    assert OPERATOR_B["email"] in emails
    assert VIEWER["email"] in emails
