"""E2E tests for RBAC asset scoping.

Tests:
1. Upload to GroupA → userA sees it, userB does not
2. No-group user uploads → personal asset visible to self + admin only
3. Admin sees all assets
4. Share asset with GroupB → userB can now see it
5. Toggle global → all users can see it
6. Unshare → group loses access again
"""
import io
import pytest
import requests
from tests.e2e.conftest import login_playwright, CMS_URL, ADMIN_USER, ADMIN_PASS


# ── Helpers ──

def _api_session(email: str, password: str) -> requests.Session:
    """Log in via HTTP and return a requests.Session."""
    s = requests.Session()
    resp = s.post(f"{CMS_URL}/login", data={"email": email, "password": password}, allow_redirects=False)
    assert resp.status_code in (302, 303), f"Login failed for {email}: {resp.status_code}"
    return s


def _upload_asset(session: requests.Session, filename: str, group_id: str | None = None) -> dict:
    """Upload a tiny PNG via API and return the asset JSON."""
    # 1x1 transparent PNG
    png_bytes = (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
        b"\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
        b"\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01"
        b"\r\n\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
    )
    url = f"{CMS_URL}/api/assets/upload"
    if group_id:
        url += f"?group_id={group_id}"
    files = {"file": (filename, io.BytesIO(png_bytes), "image/png")}
    resp = session.post(url, files=files, allow_redirects=True)
    assert resp.status_code == 201, f"Upload failed: {resp.status_code} {resp.text}"
    return resp.json()


def _list_assets(session: requests.Session) -> list[dict]:
    """List all visible assets."""
    resp = session.get(f"{CMS_URL}/api/assets/")
    assert resp.status_code == 200
    return resp.json()


def _asset_visible(session: requests.Session, asset_id: str) -> bool:
    """Check if a specific asset is visible to this session."""
    assets = _list_assets(session)
    return any(a["id"] == asset_id for a in assets)


def _delete_asset(session: requests.Session, asset_id: str):
    """Delete an asset (cleanup)."""
    session.delete(f"{CMS_URL}/api/assets/{asset_id}")


# ── Tests ──

class TestAssetGroupScoping:
    """Test that asset uploads are scoped to groups correctly."""

    def test_upload_to_group_a_visible_to_user_a(self, admin_session, test_users, groups):
        """UserA uploads to GroupA → UserA can see it."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sA, "e2e-group-a-test.png", groups["E2E-GroupA"])
        try:
            assert _asset_visible(sA, asset["id"]), "userA should see their own group's asset"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_upload_to_group_a_invisible_to_user_b(self, admin_session, test_users, groups):
        """UserA uploads to GroupA → UserB cannot see it."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        sB = _api_session(test_users["userB"]["email"], test_users["userB"]["password"])
        asset = _upload_asset(sA, "e2e-b-cant-see.png", groups["E2E-GroupA"])
        try:
            assert not _asset_visible(sB, asset["id"]), "userB should NOT see GroupA asset"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_upload_to_group_b_visible_to_user_ab(self, admin_session, test_users, groups):
        """Upload to GroupB → UserAB (in A+B) can see it."""
        sB = _api_session(test_users["userB"]["email"], test_users["userB"]["password"])
        sAB = _api_session(test_users["userAB"]["email"], test_users["userAB"]["password"])
        asset = _upload_asset(sB, "e2e-ab-sees-b.png", groups["E2E-GroupB"])
        try:
            assert _asset_visible(sAB, asset["id"]), "userAB should see GroupB asset"
        finally:
            _delete_asset(admin_session, asset["id"])


class TestPersonalAssets:
    """Test that no-group user uploads are personal (visible only to self + admin)."""

    def test_no_group_user_upload_is_personal(self, admin_session, test_users):
        """User with no groups uploads → personal asset visible to self."""
        sNone = _api_session(test_users["userNone"]["email"], test_users["userNone"]["password"])
        asset = _upload_asset(sNone, "e2e-personal.png")
        try:
            assert _asset_visible(sNone, asset["id"]), "No-group user should see own upload"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_no_group_upload_invisible_to_others(self, admin_session, test_users):
        """No-group user uploads → other users cannot see it."""
        sNone = _api_session(test_users["userNone"]["email"], test_users["userNone"]["password"])
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sNone, "e2e-personal-hidden.png")
        try:
            assert not _asset_visible(sA, asset["id"]), "userA should NOT see personal asset"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_admin_sees_personal_asset(self, admin_session, test_users):
        """Admin can see personal assets."""
        sNone = _api_session(test_users["userNone"]["email"], test_users["userNone"]["password"])
        asset = _upload_asset(sNone, "e2e-admin-sees-personal.png")
        try:
            assert _asset_visible(admin_session, asset["id"]), "Admin should see personal asset"
        finally:
            _delete_asset(admin_session, asset["id"])


class TestAdminSeeAll:
    """Test that admin sees all assets regardless of group."""

    def test_admin_sees_group_asset(self, admin_session, test_users, groups):
        """Admin sees group-scoped asset even though admin isn't in that group."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sA, "e2e-admin-sees-all.png", groups["E2E-GroupA"])
        try:
            assert _asset_visible(admin_session, asset["id"]), "Admin should see all assets"
        finally:
            _delete_asset(admin_session, asset["id"])


class TestAssetSharing:
    """Test sharing and unsharing assets between groups."""

    def test_share_makes_visible(self, admin_session, test_users, groups):
        """Sharing asset with GroupB → userB can see it."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        sB = _api_session(test_users["userB"]["email"], test_users["userB"]["password"])
        asset = _upload_asset(sA, "e2e-share-test.png", groups["E2E-GroupA"])
        try:
            # Before sharing: userB can't see it
            assert not _asset_visible(sB, asset["id"]), "Pre-share: userB shouldn't see it"

            # Share with GroupB
            resp = admin_session.post(
                f"{CMS_URL}/api/assets/{asset['id']}/share?group_id={groups['E2E-GroupB']}"
            )
            assert resp.status_code == 200

            # After sharing: userB can see it
            assert _asset_visible(sB, asset["id"]), "Post-share: userB should see it"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_unshare_removes_visibility(self, admin_session, test_users, groups):
        """Unsharing asset from GroupB → userB loses access."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        sB = _api_session(test_users["userB"]["email"], test_users["userB"]["password"])
        asset = _upload_asset(sA, "e2e-unshare-test.png", groups["E2E-GroupA"])
        try:
            # Share first
            admin_session.post(f"{CMS_URL}/api/assets/{asset['id']}/share?group_id={groups['E2E-GroupB']}")
            assert _asset_visible(sB, asset["id"]), "After share: userB should see it"

            # Unshare
            resp = admin_session.delete(
                f"{CMS_URL}/api/assets/{asset['id']}/share?group_id={groups['E2E-GroupB']}"
            )
            assert resp.status_code == 200

            # After unshare: userB can't see it
            assert not _asset_visible(sB, asset["id"]), "After unshare: userB shouldn't see it"
        finally:
            _delete_asset(admin_session, asset["id"])


class TestGlobalToggle:
    """Test that toggling an asset global makes it visible to all."""

    def test_global_toggle_makes_visible_to_all(self, admin_session, test_users, groups):
        """Making asset global → all users see it."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        sB = _api_session(test_users["userB"]["email"], test_users["userB"]["password"])
        sNone = _api_session(test_users["userNone"]["email"], test_users["userNone"]["password"])
        asset = _upload_asset(sA, "e2e-global-test.png", groups["E2E-GroupA"])
        try:
            # Before global: userB and userNone can't see it
            assert not _asset_visible(sB, asset["id"])
            assert not _asset_visible(sNone, asset["id"])

            # Toggle global
            resp = admin_session.post(f"{CMS_URL}/api/assets/{asset['id']}/global")
            assert resp.status_code == 200
            data = resp.json()
            assert data["is_global"] is True

            # After global: everyone sees it
            assert _asset_visible(sB, asset["id"]), "Global: userB should see it"
            assert _asset_visible(sNone, asset["id"]), "Global: no-group user should see it"

            # Toggle off
            resp = admin_session.post(f"{CMS_URL}/api/assets/{asset['id']}/global")
            assert resp.status_code == 200
            assert resp.json()["is_global"] is False

            # After un-global: userB loses access again
            assert not _asset_visible(sB, asset["id"]), "Un-global: userB shouldn't see it"
        finally:
            _delete_asset(admin_session, asset["id"])


class TestUIAssetVisibility:
    """Playwright tests for asset visibility in the web UI."""

    def test_user_a_sees_group_a_asset_in_ui(self, page, cms_url, admin_session, test_users, groups):
        """UserA logs in via browser and sees GroupA asset on the assets page."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sA, "e2e-ui-visible.png", groups["E2E-GroupA"])
        try:
            login_playwright(page, cms_url, test_users["userA"]["email"], test_users["userA"]["password"])
            page.goto(f"{cms_url}/assets")
            page.wait_for_selector("table")
            assert page.locator("text=e2e-ui-visible.png").count() > 0, "Asset should appear in UI"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_user_b_does_not_see_group_a_asset_in_ui(self, page, cms_url, admin_session, test_users, groups):
        """UserB logs in via browser and does NOT see GroupA asset."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sA, "e2e-ui-hidden.png", groups["E2E-GroupA"])
        try:
            login_playwright(page, cms_url, test_users["userB"]["email"], test_users["userB"]["password"])
            page.goto(f"{cms_url}/assets")
            page.wait_for_selector("table")
            assert page.locator("text=e2e-ui-hidden.png").count() == 0, "Asset should NOT appear in UI for userB"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_admin_sees_all_in_ui(self, page, cms_url, admin_session, test_users, groups):
        """Admin sees assets from any group in the UI."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sA, "e2e-ui-admin.png", groups["E2E-GroupA"])
        try:
            login_playwright(page, cms_url, "admin@localhost", ADMIN_PASS)
            page.goto(f"{cms_url}/assets")
            page.wait_for_selector("table")
            assert page.locator("text=e2e-ui-admin.png").count() > 0, "Admin should see all assets in UI"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_no_group_user_sees_personal_message(self, page, cms_url, test_users):
        """No-group user sees the personal asset info message."""
        login_playwright(page, cms_url, test_users["userNone"]["email"], test_users["userNone"]["password"])
        page.goto(f"{cms_url}/assets")
        msg = page.locator("text=not assigned to any group")
        assert msg.count() > 0, "No-group user should see personal asset message"

    def test_scope_badge_shows_correct_group(self, page, cms_url, admin_session, test_users, groups):
        """Asset uploaded to GroupA shows 'E2E-GroupA' badge in Scope column."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sA, "e2e-scope-badge.png", groups["E2E-GroupA"])
        try:
            login_playwright(page, cms_url, "admin@localhost", ADMIN_PASS)
            page.goto(f"{cms_url}/assets")
            page.wait_for_selector("table")
            # Find the row with our asset and check scope
            row = page.locator("tr", has_text="e2e-scope-badge.png")
            scope_cell = row.locator("td:nth-child(5)")  # Scope column (5th col)
            assert "E2E-GroupA" in scope_cell.text_content(), "Scope should show group name"
        finally:
            _delete_asset(admin_session, asset["id"])
