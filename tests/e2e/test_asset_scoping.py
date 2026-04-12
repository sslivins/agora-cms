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
            scope_cell = row.locator("td:nth-child(4)")  # Scope column (4th col)
            assert "E2E-GroupA" in scope_cell.text_content(), "Scope should show group name"
        finally:
            _delete_asset(admin_session, asset["id"])


class TestUIGroupPickerUpload:
    """Playwright tests for the upload panel group picker."""

    def test_upload_picker_opens_and_shows_groups(self, page, cms_url, test_users):
        """Clicking + in upload panel opens popup with available groups."""
        login_playwright(page, cms_url, test_users["userA"]["email"], test_users["userA"]["password"])
        page.goto(f"{cms_url}/assets")
        page.wait_for_selector("#upload-form")

        plus_btn = page.locator("#upload-groups-badges .btn-add-group")
        assert plus_btn.is_visible(), "Upload + button should be visible"

        plus_btn.click()
        page.wait_for_timeout(300)

        popup = page.locator("#upload-group-popup")
        assert popup.evaluate("el => getComputedStyle(el).display") == "flex", "Popup should be visible"

        items = popup.locator(".group-popup-item")
        assert items.count() > 0, "Popup should have at least one group option"

    def test_upload_picker_adds_badge(self, page, cms_url, test_users):
        """Clicking a group in the upload popup adds a badge."""
        login_playwright(page, cms_url, test_users["userA"]["email"], test_users["userA"]["password"])
        page.goto(f"{cms_url}/assets")
        page.wait_for_selector("#upload-form")

        # Count existing badges
        badges_before = page.locator("#upload-groups-badges .badge[data-group-id]").count()

        # Open popup and click first item
        page.locator("#upload-groups-badges .btn-add-group").click()
        page.wait_for_timeout(300)

        first_item = page.locator("#upload-group-popup .group-popup-item").first
        group_name = first_item.text_content().strip()
        first_item.click()
        page.wait_for_timeout(300)

        # Badge should be added
        badges_after = page.locator("#upload-groups-badges .badge[data-group-id]").count()
        assert badges_after == badges_before + 1, f"Badge count should increase: {badges_before} → {badges_after}"

        # Badge text should match group name
        new_badge = page.locator("#upload-groups-badges .badge[data-group-id]").last
        assert group_name in new_badge.text_content(), "Badge should show group name"

    def test_upload_picker_hides_already_selected(self, page, cms_url, test_users):
        """After selecting a group, it should be hidden in the popup."""
        login_playwright(page, cms_url, test_users["userAB"]["email"], test_users["userAB"]["password"])
        page.goto(f"{cms_url}/assets")
        page.wait_for_selector("#upload-form")

        # Open popup — should see multiple groups
        page.locator("#upload-groups-badges .btn-add-group").click()
        page.wait_for_timeout(300)
        items_before = page.locator("#upload-group-popup .group-popup-item:visible").count()
        assert items_before >= 2, "userAB should see at least 2 groups"

        # Pick first group
        first_item = page.locator("#upload-group-popup .group-popup-item:visible").first
        first_item.click()
        page.wait_for_timeout(300)

        # Reopen popup — should have one fewer visible item
        page.locator("#upload-groups-badges .btn-add-group").click()
        page.wait_for_timeout(300)
        items_after = page.locator("#upload-group-popup .group-popup-item:visible").count()
        assert items_after == items_before - 1, "Selected group should be hidden in popup"

    def test_upload_picker_remove_badge_restores_option(self, page, cms_url, test_users):
        """Removing a badge should re-show the group in the popup."""
        login_playwright(page, cms_url, test_users["userA"]["email"], test_users["userA"]["password"])
        page.goto(f"{cms_url}/assets")
        page.wait_for_selector("#upload-form")

        # Add a badge
        page.locator("#upload-groups-badges .btn-add-group").click()
        page.wait_for_timeout(300)
        page.locator("#upload-group-popup .group-popup-item").first.click()
        page.wait_for_timeout(300)

        badge = page.locator("#upload-groups-badges .badge[data-group-id]").first
        assert badge.count() > 0, "Badge should exist after picking"

        # Remove the badge
        badge.locator(".btn-x").click()
        page.wait_for_timeout(300)
        assert page.locator("#upload-groups-badges .badge[data-group-id]").count() == 0, "Badge should be removed"

        # Reopen popup — option should be back
        page.locator("#upload-groups-badges .btn-add-group").click()
        page.wait_for_timeout(300)
        items = page.locator("#upload-group-popup .group-popup-item:visible").count()
        assert items >= 1, "Removed group should reappear in popup"

    def test_upload_picker_plus_disabled_when_all_selected(self, page, cms_url, test_users):
        """+ button should be disabled once every available group is selected."""
        login_playwright(page, cms_url, test_users["userAB"]["email"], test_users["userAB"]["password"])
        page.goto(f"{cms_url}/assets")
        page.wait_for_selector("#upload-form")

        plus_btn = page.locator("#upload-groups-badges .btn-add-group")

        # Select all available groups one by one
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

        # All groups selected — + button should now be disabled
        assert plus_btn.is_disabled(), "+ button should be disabled when all groups are selected"


class TestUIGroupPickerLibrary:
    """Playwright tests for the library detail row group picker."""

    def _expand_asset_detail(self, page, asset_id):
        """Click an asset row to expand it and return the detail row locator."""
        row = page.locator(f'tr.asset-row[data-asset-id="{asset_id}"]')
        row.click()
        page.wait_for_timeout(400)
        detail = page.locator(f'tr.asset-detail[data-detail-for="{asset_id}"]')
        assert detail.is_visible(), "Detail row should be visible after clicking"
        return detail

    def test_library_picker_popup_visible(self, page, cms_url, admin_session, test_users, groups):
        """Clicking + in library detail opens popup with visible, clickable items."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sA, "e2e-picker-popup.png", groups["E2E-GroupA"])
        try:
            login_playwright(page, cms_url, "admin@localhost", ADMIN_PASS)
            page.goto(f"{cms_url}/assets")
            page.wait_for_selector("table")

            detail = self._expand_asset_detail(page, asset["id"])
            plus_btn = detail.locator(".btn-add-group")
            assert plus_btn.is_visible(), "Library + button should be visible"

            plus_btn.click()
            page.wait_for_timeout(400)

            popup = detail.locator(".group-popup")
            assert popup.evaluate("el => getComputedStyle(el).display") == "flex", "Popup should be displayed"

            items = popup.locator(".group-popup-item:visible")
            assert items.count() > 0, "Popup should have visible items"

            # Items should have non-zero bounding boxes (not clipped)
            first_item = items.first
            bbox = first_item.bounding_box()
            assert bbox is not None, "Popup item should have a bounding box"
            assert bbox["width"] > 0 and bbox["height"] > 0, "Popup item should have non-zero size"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_library_picker_adds_group(self, page, cms_url, admin_session, test_users, groups):
        """Clicking a group in library popup adds badge and calls share API."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sA, "e2e-picker-add.png", groups["E2E-GroupA"])
        try:
            login_playwright(page, cms_url, "admin@localhost", ADMIN_PASS)
            page.goto(f"{cms_url}/assets")
            page.wait_for_selector("table")

            detail = self._expand_asset_detail(page, asset["id"])
            scope_el = detail.locator(f'#scope-{asset["id"]}')
            badges_before = scope_el.locator(".badge[data-group-id]").count()

            # Open popup and click an available group
            detail.locator(".btn-add-group").click()
            page.wait_for_timeout(400)

            available = detail.locator(".group-popup-item:visible")
            assert available.count() > 0, "Should have groups to add"
            group_name = available.first.text_content().strip()

            # Intercept the share API call
            with page.expect_response(lambda r: "/share" in r.url and r.status == 200):
                available.first.click()

            page.wait_for_timeout(500)

            # Badge should be added
            badges_after = scope_el.locator(".badge[data-group-id]").count()
            assert badges_after == badges_before + 1, f"Badge count should increase: {badges_before} → {badges_after}"

            # Badge text should match
            all_badge_text = scope_el.locator(".badge[data-group-id]").all_text_contents()
            assert any(group_name in t for t in all_badge_text), f"New badge with '{group_name}' should appear"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_library_picker_remove_group(self, page, cms_url, admin_session, test_users, groups):
        """Removing a group badge calls unshare API and updates the UI."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        # Upload to GroupA, then share with GroupB via API
        asset = _upload_asset(sA, "e2e-picker-remove.png", groups["E2E-GroupA"])
        admin_session.post(f"{CMS_URL}/api/assets/{asset['id']}/share?group_id={groups['E2E-GroupB']}")
        try:
            login_playwright(page, cms_url, "admin@localhost", ADMIN_PASS)
            page.goto(f"{cms_url}/assets")
            page.wait_for_selector("table")

            detail = self._expand_asset_detail(page, asset["id"])
            scope_el = detail.locator(f'#scope-{asset["id"]}')
            badges_before = scope_el.locator(".badge[data-group-id]").count()
            assert badges_before >= 2, "Should have at least 2 group badges"

            # Click × on the last badge
            last_badge = scope_el.locator(".badge[data-group-id]").last
            badge_gid = last_badge.get_attribute("data-group-id")

            with page.expect_response(lambda r: "/share" in r.url and r.status == 200):
                last_badge.locator(".btn-x").click()

            page.wait_for_timeout(500)

            badges_after = scope_el.locator(".badge[data-group-id]").count()
            assert badges_after == badges_before - 1, f"Badge count should decrease: {badges_before} → {badges_after}"

            # Verify via API that the group was actually unshared
            resp = admin_session.get(f"{CMS_URL}/api/assets/{asset['id']}")
            asset_data = resp.json()
            remaining_groups = [ga["group_id"] for ga in asset_data.get("group_asset_entries", [])]
            assert badge_gid not in remaining_groups, "Removed group should not be in asset's groups"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_library_picker_no_overflow_clip(self, page, cms_url, admin_session, test_users, groups):
        """The scope cell should not clip the popup due to overflow:hidden."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sA, "e2e-picker-overflow.png", groups["E2E-GroupA"])
        try:
            login_playwright(page, cms_url, "admin@localhost", ADMIN_PASS)
            page.goto(f"{cms_url}/assets")
            page.wait_for_selector("table")

            detail = self._expand_asset_detail(page, asset["id"])
            scope_el = detail.locator(f'#scope-{asset["id"]}')

            # Check that the scope element doesn't have overflow:hidden
            overflow = scope_el.evaluate("el => getComputedStyle(el).overflow")
            assert overflow != "hidden", f"Scope should not have overflow:hidden, got {overflow}"

            # Open popup and verify it's not clipped
            detail.locator(".btn-add-group").click()
            page.wait_for_timeout(400)

            popup = detail.locator(".group-popup")
            popup_box = popup.bounding_box()
            scope_box = scope_el.bounding_box()
            assert popup_box is not None, "Popup should have a bounding box (not clipped)"
            # Popup opens upward, so its top should be above the scope element
            assert popup_box["y"] < scope_box["y"], "Popup should extend above the scope element"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_library_collapsed_row_syncs(self, page, cms_url, admin_session, test_users, groups):
        """After adding a group in detail view, the collapsed row scope should update."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sA, "e2e-collapsed-sync.png", groups["E2E-GroupA"])
        try:
            login_playwright(page, cms_url, "admin@localhost", ADMIN_PASS)
            page.goto(f"{cms_url}/assets")
            page.wait_for_selector("table")

            # Check collapsed row has GroupA badge
            collapsed_row = page.locator(f'tr.asset-row[data-asset-id="{asset["id"]}"]')
            scope_cell = collapsed_row.locator("td:nth-child(4)")
            assert "E2E-GroupA" in scope_cell.text_content(), "Collapsed row should show GroupA"

            # Expand and add GroupB
            detail = self._expand_asset_detail(page, asset["id"])
            detail.locator(".btn-add-group").click()
            page.wait_for_timeout(400)

            # Find E2E-GroupB in popup
            group_b_btn = detail.locator('.group-popup-item', has_text="E2E-GroupB")
            if group_b_btn.count() > 0:
                with page.expect_response(lambda r: "/share" in r.url and r.status == 200):
                    group_b_btn.click()
                page.wait_for_timeout(500)

                # Collapse the row
                collapsed_row.click()
                page.wait_for_timeout(300)

                # Re-read scope cell — should now include GroupB
                scope_text = scope_cell.text_content()
                assert "E2E-GroupB" in scope_text or "+1" in scope_text, \
                    f"Collapsed row should show GroupB or +N more overflow, got: {scope_text}"
        finally:
            _delete_asset(admin_session, asset["id"])

    def test_library_picker_plus_disabled_when_all_selected(self, page, cms_url, admin_session, test_users, groups):
        """+ button should be disabled once every group is assigned to the asset."""
        sA = _api_session(test_users["userA"]["email"], test_users["userA"]["password"])
        asset = _upload_asset(sA, "e2e-picker-allsel.png", groups["E2E-GroupA"])
        try:
            login_playwright(page, cms_url, "admin@localhost", ADMIN_PASS)
            page.goto(f"{cms_url}/assets")
            page.wait_for_selector("table")

            detail = self._expand_asset_detail(page, asset["id"])
            plus_btn = detail.locator(".btn-add-group")

            # Keep adding groups until + becomes disabled
            for _ in range(20):  # safety cap
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
            _delete_asset(admin_session, asset["id"])
