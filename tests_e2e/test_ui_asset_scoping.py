"""Playwright tests for RBAC asset scoping in the web UI.

Covers:
- TestUIAssetVisibility: asset visibility per user/group in the UI
- TestUIGroupPickerUpload: upload panel group-picker interactions
- TestUIGroupPickerLibrary: library detail-row group-picker interactions
- TestSchedulerGroupScoping: scheduler target dropdown scoping

Converted from tests/e2e/test_asset_scoping.py (19 Playwright tests).
API-only tests are covered by tests/test_rbac.py and NOT duplicated here.
"""

import io

import httpx
import pytest
from playwright.sync_api import Browser, BrowserContext, Page

# ── Tiny 1×1 transparent PNG used by all upload helpers ──

_PNG_1X1 = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
    b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
    b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
    b"\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
)


# ── Helpers ──


def _admin_cookies(base_url: str) -> dict[str, str]:
    """Log in as admin via HTTP and return cookies dict."""
    with httpx.Client(base_url=base_url, follow_redirects=True, timeout=10) as c:
        c.post("/login", data={"email": "admin", "password": "testpass"})
        return dict(c.cookies)


def _login_as(
    browser_instance: Browser,
    base_url: str,
    email: str,
    password: str,
) -> tuple[BrowserContext, Page]:
    """Create a new BrowserContext, log in via the web form, and return (ctx, page)."""
    ctx = browser_instance.new_context(
        base_url=base_url,
        ignore_https_errors=True,
        viewport={"width": 1280, "height": 1024},
    )
    page = ctx.new_page()
    page.goto("/login")
    page.fill('input[name="email"]', email)
    page.fill('input[name="password"]', password)
    page.click('button[type="submit"]')
    page.wait_for_url("**/")
    return ctx, page


def _upload_asset_api(
    base_url: str,
    cookies: dict[str, str],
    filename: str,
    group_id: str | None = None,
) -> dict:
    """Upload a 1×1 PNG via the REST API and return the asset JSON."""
    url = f"{base_url}/api/assets/upload"
    if group_id:
        url += f"?group_id={group_id}"
    with httpx.Client(cookies=cookies, timeout=10, follow_redirects=True) as c:
        resp = c.post(url, files={"file": (filename, io.BytesIO(_PNG_1X1), "image/png")})
    assert resp.status_code == 201, f"Upload failed: {resp.status_code} {resp.text}"
    return resp.json()


def _delete_asset_api(base_url: str, cookies: dict[str, str], asset_id: str):
    """Delete an asset (cleanup)."""
    with httpx.Client(cookies=cookies, timeout=10) as c:
        c.delete(f"{base_url}/api/assets/{asset_id}")


def _list_assets_api(base_url: str, cookies: dict[str, str]) -> list[dict]:
    """List all visible assets."""
    with httpx.Client(cookies=cookies, timeout=10) as c:
        resp = c.get(f"{base_url}/api/assets/")
    assert resp.status_code == 200
    return resp.json()


def _user_cookies(base_url: str, email: str, password: str) -> dict[str, str]:
    """Log in as a regular user via HTTP and return cookies dict."""
    with httpx.Client(base_url=base_url, follow_redirects=True, timeout=10) as c:
        c.post("/login", data={"email": email, "password": password})
        return dict(c.cookies)


# ── Session-scoped fixtures ──


@pytest.fixture(scope="session")
def admin_cookies(e2e_server, base_url) -> dict[str, str]:
    """Admin cookie jar shared across the whole session."""
    return _admin_cookies(base_url)


@pytest.fixture(scope="session")
def scoping_groups(e2e_server, base_url, admin_cookies) -> dict[str, str]:
    """Create E2E-GroupA and E2E-GroupB; return ``{name: id}``."""
    result = {}
    with httpx.Client(base_url=base_url, cookies=admin_cookies, timeout=10) as c:
        existing = c.get("/api/devices/groups/").json()
        existing_map = {g["name"]: g["id"] for g in existing}
        for name in ("E2E-GroupA", "E2E-GroupB"):
            if name in existing_map:
                result[name] = existing_map[name]
            else:
                resp = c.post("/api/devices/groups/", json={"name": name})
                assert resp.status_code == 201, f"Create group {name}: {resp.status_code} {resp.text}"
                result[name] = resp.json()["id"]
    return result


@pytest.fixture(scope="session")
def scoping_users(
    e2e_server, base_url, admin_cookies, scoping_groups,
) -> dict[str, dict]:
    """Create four test users and return ``{name: {id, email, password}}``."""
    groups = scoping_groups
    with httpx.Client(base_url=base_url, cookies=admin_cookies, timeout=10) as c:
        # Seed SMTP settings so the user-creation endpoint is unblocked
        c.post("/api/settings/smtp", json={
            "host": "smtp.fake.local",
            "port": 587,
            "from_email": "noreply@fake.local",
        })

        # Look up the Operator role id
        roles_resp = c.get("/api/roles")
        assert roles_resp.status_code == 200
        roles = {r["name"]: r["id"] for r in roles_resp.json()}
        operator_role_id = roles["Operator"]

        specs = {
            "userA":    {"email": "e2e-usera@test.local",    "groups": [groups["E2E-GroupA"]]},
            "userB":    {"email": "e2e-userb@test.local",    "groups": [groups["E2E-GroupB"]]},
            "userAB":   {"email": "e2e-userab@test.local",   "groups": [groups["E2E-GroupA"], groups["E2E-GroupB"]]},
            "userNone": {"email": "e2e-usernone@test.local", "groups": []},
        }
        users: dict[str, dict] = {}
        for name, spec in specs.items():
            password = f"TestPass{name}123!"
            resp = c.post("/api/users", json={
                "email": spec["email"],
                "display_name": name,
                "password": password,
                "role_id": operator_role_id,
                "group_ids": spec["groups"],
            })
            if resp.status_code == 201:
                uid = resp.json()["id"]
            elif resp.status_code == 409:
                all_users = c.get("/api/users").json()
                uid = next(u["id"] for u in all_users if u["email"] == spec["email"])
            else:
                raise AssertionError(f"Create user {name}: {resp.status_code} {resp.text}")

            # Ensure correct groups/password and disable forced password change
            c.patch(f"/api/users/{uid}", json={
                "group_ids": spec["groups"],
                "password": password,
                "must_change_password": False,
            })
            users[name] = {"id": uid, "email": spec["email"], "password": password}
    return users


# ── TestUIAssetVisibility (5 tests) ──


class TestUIAssetVisibility:
    """Asset visibility in the web UI based on group membership."""

    def test_user_a_sees_group_a_asset_in_ui(
        self, e2e_server, base_url, browser_instance, admin_cookies,
        scoping_groups, scoping_users,
    ):
        """UserA logs in via browser and sees GroupA asset on the assets page."""
        user = scoping_users["userA"]
        user_cookies = _user_cookies(base_url, user["email"], user["password"])
        asset = _upload_asset_api(base_url, user_cookies, "e2e-ui-visible.png", scoping_groups["E2E-GroupA"])
        ctx = page = None
        try:
            ctx, page = _login_as(browser_instance, base_url, user["email"], user["password"])
            page.goto("/assets")
            page.wait_for_selector("table")
            assert page.locator("text=e2e-ui-visible.png").count() > 0, "Asset should appear in UI"
        finally:
            _delete_asset_api(base_url, admin_cookies, asset["id"])
            if page:
                page.close()
            if ctx:
                ctx.close()

    def test_user_b_does_not_see_group_a_asset_in_ui(
        self, e2e_server, base_url, browser_instance, admin_cookies,
        scoping_groups, scoping_users,
    ):
        """UserB logs in via browser and does NOT see GroupA asset."""
        userA = scoping_users["userA"]
        userB = scoping_users["userB"]
        userA_cookies = _user_cookies(base_url, userA["email"], userA["password"])
        asset = _upload_asset_api(base_url, userA_cookies, "e2e-ui-hidden.png", scoping_groups["E2E-GroupA"])
        ctx = page = None
        try:
            ctx, page = _login_as(browser_instance, base_url, userB["email"], userB["password"])
            page.goto("/assets")
            page.wait_for_selector("table")
            assert page.locator("text=e2e-ui-hidden.png").count() == 0, \
                "Asset should NOT appear in UI for userB"
        finally:
            _delete_asset_api(base_url, admin_cookies, asset["id"])
            if page:
                page.close()
            if ctx:
                ctx.close()

    def test_admin_sees_all_in_ui(
        self, e2e_server, base_url, browser_instance, admin_cookies,
        scoping_groups, scoping_users,
    ):
        """Admin sees assets from any group in the UI."""
        userA = scoping_users["userA"]
        userA_cookies = _user_cookies(base_url, userA["email"], userA["password"])
        asset = _upload_asset_api(base_url, userA_cookies, "e2e-ui-admin.png", scoping_groups["E2E-GroupA"])
        ctx = page = None
        try:
            ctx, page = _login_as(browser_instance, base_url, "admin", "testpass")
            page.goto("/assets")
            page.wait_for_selector("table")
            assert page.locator("text=e2e-ui-admin.png").count() > 0, \
                "Admin should see all assets in UI"
        finally:
            _delete_asset_api(base_url, admin_cookies, asset["id"])
            if page:
                page.close()
            if ctx:
                ctx.close()

    def test_no_group_user_sees_personal_message(
        self, e2e_server, base_url, browser_instance,
        scoping_groups, scoping_users,
    ):
        """No-group user sees the personal-asset info message."""
        user = scoping_users["userNone"]
        ctx, page = _login_as(browser_instance, base_url, user["email"], user["password"])
        try:
            page.goto("/assets")
            msg = page.locator("text=not assigned to any group")
            assert msg.count() > 0, "No-group user should see personal asset message"
        finally:
            page.close()
            ctx.close()

    def test_scope_badge_shows_correct_group(
        self, e2e_server, base_url, browser_instance, admin_cookies,
        scoping_groups, scoping_users,
    ):
        """Asset uploaded to GroupA shows 'E2E-GroupA' badge in Scope column."""
        userA = scoping_users["userA"]
        userA_cookies = _user_cookies(base_url, userA["email"], userA["password"])
        asset = _upload_asset_api(base_url, userA_cookies, "e2e-scope-badge.png", scoping_groups["E2E-GroupA"])
        ctx = page = None
        try:
            ctx, page = _login_as(browser_instance, base_url, "admin", "testpass")
            page.goto("/assets")
            page.wait_for_selector("table")
            row = page.locator("tr", has_text="e2e-scope-badge.png")
            scope_cell = row.locator("td:nth-child(4)")
            assert "E2E-GroupA" in scope_cell.text_content(), "Scope should show group name"
        finally:
            _delete_asset_api(base_url, admin_cookies, asset["id"])
            if page:
                page.close()
            if ctx:
                ctx.close()


# ── TestUIGroupPickerUpload (6 tests) ──


class TestUIGroupPickerUpload:
    """Upload panel group-picker behaviour."""

    def test_upload_button_disabled_until_file_selected(
        self, e2e_server, base_url, browser_instance,
        scoping_groups, scoping_users,
    ):
        """Upload button should be disabled when no file is selected."""
        user = scoping_users["userA"]
        ctx, page = _login_as(browser_instance, base_url, user["email"], user["password"])
        try:
            page.goto("/assets")
            page.wait_for_selector("#upload-form")

            submit_btn = page.locator('#upload-form button[type="submit"]')
            assert submit_btn.is_disabled(), "Upload button should be disabled when no file is selected"

            file_input = page.locator("#file-input")
            file_input.set_input_files({
                "name": "test-upload-btn.png",
                "mimeType": "image/png",
                "buffer": _PNG_1X1,
            })
            page.wait_for_timeout(300)
            assert not submit_btn.is_disabled(), "Upload button should be enabled after file is selected"
        finally:
            page.close()
            ctx.close()

    def test_upload_picker_opens_and_shows_groups(
        self, e2e_server, base_url, browser_instance,
        scoping_groups, scoping_users,
    ):
        """Clicking + in upload panel opens popup with available groups."""
        user = scoping_users["userA"]
        ctx, page = _login_as(browser_instance, base_url, user["email"], user["password"])
        try:
            page.goto("/assets")
            page.wait_for_selector("#upload-form")

            plus_btn = page.locator("#upload-groups-badges .btn-add-group")
            assert plus_btn.is_visible(), "Upload + button should be visible"

            plus_btn.click()
            page.wait_for_timeout(300)

            popup = page.locator("#upload-group-popup")
            assert popup.evaluate("el => getComputedStyle(el).display") == "flex", "Popup should be visible"

            items = popup.locator(".group-popup-item")
            assert items.count() > 0, "Popup should have at least one group option"
        finally:
            page.close()
            ctx.close()

    def test_upload_picker_adds_badge(
        self, e2e_server, base_url, browser_instance,
        scoping_groups, scoping_users,
    ):
        """Clicking a group in the upload popup adds a badge."""
        user = scoping_users["userA"]
        ctx, page = _login_as(browser_instance, base_url, user["email"], user["password"])
        try:
            page.goto("/assets")
            page.wait_for_selector("#upload-form")

            badges_before = page.locator("#upload-groups-badges .badge[data-group-id]").count()

            page.locator("#upload-groups-badges .btn-add-group").click()
            page.wait_for_timeout(300)

            first_item = page.locator("#upload-group-popup .group-popup-item").first
            group_name = first_item.text_content().strip()
            first_item.click()
            page.wait_for_timeout(300)

            badges_after = page.locator("#upload-groups-badges .badge[data-group-id]").count()
            assert badges_after == badges_before + 1, \
                f"Badge count should increase: {badges_before} → {badges_after}"

            new_badge = page.locator("#upload-groups-badges .badge[data-group-id]").last
            assert group_name in new_badge.text_content(), "Badge should show group name"
        finally:
            page.close()
            ctx.close()

    def test_upload_picker_hides_already_selected(
        self, e2e_server, base_url, browser_instance,
        scoping_groups, scoping_users,
    ):
        """After selecting a group it should be hidden in the popup."""
        user = scoping_users["userAB"]
        ctx, page = _login_as(browser_instance, base_url, user["email"], user["password"])
        try:
            page.goto("/assets")
            page.wait_for_selector("#upload-form")

            page.locator("#upload-groups-badges .btn-add-group").click()
            page.wait_for_timeout(300)
            items_before = page.locator("#upload-group-popup .group-popup-item:visible").count()
            assert items_before >= 2, "userAB should see at least 2 groups"

            page.locator("#upload-group-popup .group-popup-item:visible").first.click()
            page.wait_for_timeout(300)

            page.locator("#upload-groups-badges .btn-add-group").click()
            page.wait_for_timeout(300)
            items_after = page.locator("#upload-group-popup .group-popup-item:visible").count()
            assert items_after == items_before - 1, "Selected group should be hidden in popup"
        finally:
            page.close()
            ctx.close()

    def test_upload_picker_remove_badge_restores_option(
        self, e2e_server, base_url, browser_instance,
        scoping_groups, scoping_users,
    ):
        """Removing a badge should re-show the group in the popup."""
        user = scoping_users["userA"]
        ctx, page = _login_as(browser_instance, base_url, user["email"], user["password"])
        try:
            page.goto("/assets")
            page.wait_for_selector("#upload-form")

            page.locator("#upload-groups-badges .btn-add-group").click()
            page.wait_for_timeout(300)
            page.locator("#upload-group-popup .group-popup-item").first.click()
            page.wait_for_timeout(300)

            badge = page.locator("#upload-groups-badges .badge[data-group-id]").first
            assert badge.count() > 0, "Badge should exist after picking"

            badge.locator(".btn-x").click()
            page.wait_for_timeout(300)
            assert page.locator("#upload-groups-badges .badge[data-group-id]").count() == 0, \
                "Badge should be removed"

            page.locator("#upload-groups-badges .btn-add-group").click()
            page.wait_for_timeout(300)
            items = page.locator("#upload-group-popup .group-popup-item:visible").count()
            assert items >= 1, "Removed group should reappear in popup"
        finally:
            page.close()
            ctx.close()

    def test_upload_picker_plus_disabled_when_all_selected(
        self, e2e_server, base_url, browser_instance,
        scoping_groups, scoping_users,
    ):
        """+ button should be disabled once every available group is selected."""
        user = scoping_users["userAB"]
        ctx, page = _login_as(browser_instance, base_url, user["email"], user["password"])
        try:
            page.goto("/assets")
            page.wait_for_selector("#upload-form")

            plus_btn = page.locator("#upload-groups-badges .btn-add-group")

            while True:
                if plus_btn.is_disabled():
                    break
                plus_btn.click()
                page.wait_for_timeout(300)
                visible_items = page.locator("#upload-group-popup .group-popup-item:visible")
                if visible_items.count() == 0:
                    page.evaluate("closeAllGroupPopups()")
                    break
                visible_items.first.click()
                page.wait_for_timeout(300)

            assert plus_btn.is_disabled(), \
                "+ button should be disabled when all groups are selected"
        finally:
            page.close()
            ctx.close()


# ── TestUIGroupPickerLibrary (7 tests) ──


def _expand_asset_detail(page: Page, asset_id: str):
    """Click an asset row to expand it and return the detail-row locator."""
    row = page.locator(f'tr.asset-row[data-asset-id="{asset_id}"]')
    row.click()
    page.wait_for_timeout(400)
    detail = page.locator(f'tr.asset-detail[data-detail-for="{asset_id}"]')
    assert detail.is_visible(), "Detail row should be visible after clicking"
    return detail


class TestUIGroupPickerLibrary:
    """Library detail-row group-picker interactions."""

    def test_library_picker_popup_visible(
        self, e2e_server, base_url, browser_instance, admin_cookies,
        scoping_groups, scoping_users,
    ):
        """Clicking + in library detail opens popup with visible, clickable items."""
        userA = scoping_users["userA"]
        userA_cookies = _user_cookies(base_url, userA["email"], userA["password"])
        asset = _upload_asset_api(base_url, userA_cookies, "e2e-picker-popup.png", scoping_groups["E2E-GroupA"])
        ctx = page = None
        try:
            ctx, page = _login_as(browser_instance, base_url, "admin", "testpass")
            page.goto("/assets")
            page.wait_for_selector("table")

            detail = _expand_asset_detail(page, asset["id"])
            plus_btn = detail.locator(".btn-add-group")
            assert plus_btn.is_visible(), "Library + button should be visible"

            plus_btn.click()
            page.wait_for_timeout(400)

            popup = detail.locator(".group-popup")
            assert popup.evaluate("el => getComputedStyle(el).display") == "flex", \
                "Popup should be displayed"

            items = popup.locator(".group-popup-item:visible")
            assert items.count() > 0, "Popup should have visible items"

            first_item = items.first
            bbox = first_item.bounding_box()
            assert bbox is not None, "Popup item should have a bounding box"
            assert bbox["width"] > 0 and bbox["height"] > 0, \
                "Popup item should have non-zero size"
        finally:
            _delete_asset_api(base_url, admin_cookies, asset["id"])
            if page:
                page.close()
            if ctx:
                ctx.close()

    def test_library_picker_adds_group(
        self, e2e_server, base_url, browser_instance, admin_cookies,
        scoping_groups, scoping_users,
    ):
        """Clicking a group in library popup adds badge and calls share API."""
        userA = scoping_users["userA"]
        userA_cookies = _user_cookies(base_url, userA["email"], userA["password"])
        asset = _upload_asset_api(base_url, userA_cookies, "e2e-picker-add.png", scoping_groups["E2E-GroupA"])
        ctx = page = None
        try:
            ctx, page = _login_as(browser_instance, base_url, "admin", "testpass")
            page.goto("/assets")
            page.wait_for_selector("table")

            detail = _expand_asset_detail(page, asset["id"])
            scope_el = detail.locator(f'#scope-{asset["id"]}')
            badges_before = scope_el.locator(".badge[data-group-id]").count()

            detail.locator(".btn-add-group").click()
            page.wait_for_timeout(400)

            available = detail.locator(".group-popup-item:visible")
            assert available.count() > 0, "Should have groups to add"
            group_name = available.first.text_content().strip()

            with page.expect_response(lambda r: "/share" in r.url and r.status == 200):
                available.first.click()

            page.wait_for_timeout(500)

            badges_after = scope_el.locator(".badge[data-group-id]").count()
            assert badges_after == badges_before + 1, \
                f"Badge count should increase: {badges_before} → {badges_after}"

            all_badge_text = scope_el.locator(".badge[data-group-id]").all_text_contents()
            assert any(group_name in t for t in all_badge_text), \
                f"New badge with '{group_name}' should appear"
        finally:
            _delete_asset_api(base_url, admin_cookies, asset["id"])
            if page:
                page.close()
            if ctx:
                ctx.close()

    def test_library_picker_remove_group(
        self, e2e_server, base_url, browser_instance, admin_cookies,
        scoping_groups, scoping_users,
    ):
        """Removing a group badge calls unshare API and updates the UI."""
        userA = scoping_users["userA"]
        userA_cookies = _user_cookies(base_url, userA["email"], userA["password"])
        asset = _upload_asset_api(base_url, userA_cookies, "e2e-picker-remove.png", scoping_groups["E2E-GroupA"])

        # Share with GroupB via API so the asset has 2 groups
        with httpx.Client(base_url=base_url, cookies=admin_cookies, timeout=10) as c:
            c.post(f"/api/assets/{asset['id']}/share?group_id={scoping_groups['E2E-GroupB']}")

        ctx = page = None
        try:
            ctx, page = _login_as(browser_instance, base_url, "admin", "testpass")
            page.goto("/assets")
            page.wait_for_selector("table")

            detail = _expand_asset_detail(page, asset["id"])
            scope_el = detail.locator(f'#scope-{asset["id"]}')
            badges_before = scope_el.locator(".badge[data-group-id]").count()
            assert badges_before >= 2, "Should have at least 2 group badges"

            last_badge = scope_el.locator(".badge[data-group-id]").last
            badge_gid = last_badge.get_attribute("data-group-id")

            with page.expect_response(lambda r: "/share" in r.url and r.status == 200):
                last_badge.locator(".btn-x").click()
                page.locator(".modal-overlay .btn-danger", has_text="Confirm").click()

            page.wait_for_timeout(500)

            badges_after = scope_el.locator(".badge[data-group-id]").count()
            assert badges_after == badges_before - 1, \
                f"Badge count should decrease: {badges_before} → {badges_after}"

            # Verify via API
            with httpx.Client(base_url=base_url, cookies=admin_cookies, timeout=10) as c:
                resp = c.get(f"/api/assets/{asset['id']}")
                asset_data = resp.json()
            remaining = [ga["group_id"] for ga in asset_data.get("group_asset_entries", [])]
            assert badge_gid not in remaining, "Removed group should not be in asset's groups"
        finally:
            _delete_asset_api(base_url, admin_cookies, asset["id"])
            if page:
                page.close()
            if ctx:
                ctx.close()

    def test_library_picker_no_overflow_clip(
        self, e2e_server, base_url, browser_instance, admin_cookies,
        scoping_groups, scoping_users,
    ):
        """The scope cell should not clip the popup due to overflow:hidden."""
        userA = scoping_users["userA"]
        userA_cookies = _user_cookies(base_url, userA["email"], userA["password"])
        asset = _upload_asset_api(base_url, userA_cookies, "e2e-picker-overflow.png", scoping_groups["E2E-GroupA"])
        ctx = page = None
        try:
            ctx, page = _login_as(browser_instance, base_url, "admin", "testpass")
            page.goto("/assets")
            page.wait_for_selector("table")

            detail = _expand_asset_detail(page, asset["id"])
            scope_el = detail.locator(f'#scope-{asset["id"]}')

            overflow = scope_el.evaluate("el => getComputedStyle(el).overflow")
            assert overflow != "hidden", f"Scope should not have overflow:hidden, got {overflow}"

            detail.locator(".btn-add-group").click()
            page.wait_for_timeout(400)

            popup = detail.locator(".group-popup")
            popup_box = popup.bounding_box()
            scope_box = scope_el.bounding_box()
            assert popup_box is not None, "Popup should have a bounding box (not clipped)"
            assert popup_box["y"] < scope_box["y"], \
                "Popup should extend above the scope element"
        finally:
            _delete_asset_api(base_url, admin_cookies, asset["id"])
            if page:
                page.close()
            if ctx:
                ctx.close()

    def test_library_collapsed_row_syncs(
        self, e2e_server, base_url, browser_instance, admin_cookies,
        scoping_groups, scoping_users,
    ):
        """After adding a group in detail view, the collapsed row scope should update."""
        userA = scoping_users["userA"]
        userA_cookies = _user_cookies(base_url, userA["email"], userA["password"])
        asset = _upload_asset_api(base_url, userA_cookies, "e2e-collapsed-sync.png", scoping_groups["E2E-GroupA"])
        ctx = page = None
        try:
            ctx, page = _login_as(browser_instance, base_url, "admin", "testpass")
            page.goto("/assets")
            page.wait_for_selector("table")

            collapsed_row = page.locator(f'tr.asset-row[data-asset-id="{asset["id"]}"]')
            scope_cell = collapsed_row.locator("td:nth-child(4)")
            assert "E2E-GroupA" in scope_cell.text_content(), "Collapsed row should show GroupA"

            detail = _expand_asset_detail(page, asset["id"])
            detail.locator(".btn-add-group").click()
            page.wait_for_timeout(400)

            group_b_btn = detail.locator(".group-popup-item", has_text="E2E-GroupB")
            if group_b_btn.count() > 0:
                with page.expect_response(lambda r: "/share" in r.url and r.status == 200):
                    group_b_btn.click()
                page.wait_for_timeout(500)

                collapsed_row.click()
                page.wait_for_timeout(300)

                scope_text = scope_cell.text_content()
                assert "E2E-GroupB" in scope_text or "+1" in scope_text, \
                    f"Collapsed row should show GroupB or +N more overflow, got: {scope_text}"
        finally:
            _delete_asset_api(base_url, admin_cookies, asset["id"])
            if page:
                page.close()
            if ctx:
                ctx.close()

    def test_library_picker_plus_disabled_when_all_selected(
        self, e2e_server, base_url, browser_instance, admin_cookies,
        scoping_groups, scoping_users,
    ):
        """+ button should be disabled once every group is assigned to the asset."""
        userA = scoping_users["userA"]
        userA_cookies = _user_cookies(base_url, userA["email"], userA["password"])
        asset = _upload_asset_api(base_url, userA_cookies, "e2e-picker-allsel.png", scoping_groups["E2E-GroupA"])
        ctx = page = None
        try:
            ctx, page = _login_as(browser_instance, base_url, "admin", "testpass")
            page.goto("/assets")
            page.wait_for_selector("table")

            detail = _expand_asset_detail(page, asset["id"])
            plus_btn = detail.locator(".btn-add-group")

            for _ in range(20):
                if plus_btn.is_disabled():
                    break
                plus_btn.click()
                page.wait_for_timeout(400)
                visible = detail.locator(".group-popup-item:visible")
                if visible.count() == 0:
                    page.evaluate("closeAllGroupPopups()")
                    page.wait_for_timeout(300)
                    break
                with page.expect_response(lambda r: "/share" in r.url and r.status == 200):
                    visible.first.click()
                page.wait_for_timeout(500)

            assert plus_btn.is_disabled(), \
                "Library + button should be disabled when all groups are assigned"
        finally:
            _delete_asset_api(base_url, admin_cookies, asset["id"])
            if page:
                page.close()
            if ctx:
                ctx.close()

    def test_library_picker_hidden_when_global(
        self, e2e_server, base_url, browser_instance, admin_cookies,
        scoping_groups, scoping_users,
    ):
        """+ button should not be visible when an asset is marked global."""
        userA = scoping_users["userA"]
        userA_cookies = _user_cookies(base_url, userA["email"], userA["password"])
        asset = _upload_asset_api(base_url, userA_cookies, "e2e-picker-global.png", scoping_groups["E2E-GroupA"])

        # Make asset global via API
        with httpx.Client(base_url=base_url, cookies=admin_cookies, timeout=10) as c:
            c.post(f"/api/assets/{asset['id']}/global")

        ctx = page = None
        try:
            ctx, page = _login_as(browser_instance, base_url, "admin", "testpass")
            page.goto("/assets")
            page.wait_for_selector("table")

            detail = _expand_asset_detail(page, asset["id"])

            plus_btn = detail.locator(".btn-add-group")
            assert plus_btn.count() == 0 or not plus_btn.is_visible(), \
                "Group + button should be hidden when asset is global"

            scope_el = detail.locator(f'#scope-{asset["id"]}')
            assert "Global" in scope_el.text_content(), "Should show Global badge"
        finally:
            # Un-global before cleanup
            with httpx.Client(base_url=base_url, cookies=admin_cookies, timeout=10) as c:
                c.post(f"/api/assets/{asset['id']}/global")
            _delete_asset_api(base_url, admin_cookies, asset["id"])
            if page:
                page.close()
            if ctx:
                ctx.close()


# ── TestSchedulerGroupScoping (1 test) ──


class TestSchedulerGroupScoping:
    """Scheduler page should only show groups the user has access to."""

    def test_scheduler_target_shows_only_user_groups(
        self, e2e_server, base_url, browser_instance,
        scoping_groups, scoping_users,
    ):
        """When a non-admin user selects 'Group' as target type in the scheduler,
        only their assigned groups should appear in the target dropdown."""
        user = scoping_users["userA"]
        ctx, page = _login_as(browser_instance, base_url, user["email"], user["password"])
        try:
            page.goto("/schedules")

            target_select = page.locator("#target_id")
            options = target_select.locator("option").all()
            option_texts = [o.text_content().strip() for o in options]

            assert "E2E-GroupA" in option_texts, \
                f"Expected E2E-GroupA in target options, got: {option_texts}"
            assert "E2E-GroupB" not in option_texts, \
                f"E2E-GroupB should not be visible to userA, got: {option_texts}"
        finally:
            page.close()
            ctx.close()
