"""Playwright tests for device group Remove buttons."""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async
from tests_e2e.fake_device import FakeDevice


class TestGroupRemoveButtons:
    """Remove buttons in the Device Groups panel."""

    def _register_and_adopt(self, api, ws_url, device_id, name=None):
        """Register a fake device and adopt it via the API."""
        async def register():
            async with FakeDevice(device_id, ws_url, device_name=name) as dev:
                await dev.send_status()

        run_async(register())
        api.post(f"/api/devices/{device_id}/adopt")

    def test_group_remove_ungroups_all_devices(self, page: Page, api, ws_url, e2e_server):
        """Clicking Remove on a group should delete the group and ungroup all its devices."""
        # Register and adopt two devices
        self._register_and_adopt(api, ws_url, "grp-rm-001", "Device A")
        self._register_and_adopt(api, ws_url, "grp-rm-002", "Device B")

        # Create a group
        resp = api.post("/api/devices/groups/", json={"name": "Remove Test Group"})
        assert resp.status_code == 201, f"Group create failed {resp.status_code}: {resp.text}"
        group_id = resp.json()["id"]

        # Assign both devices to the group
        api.patch("/api/devices/grp-rm-001", json={"group_id": group_id})
        api.patch("/api/devices/grp-rm-002", json={"group_id": group_id})

        # Load the page and verify the group exists
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        expect(page.locator("strong", has_text="Remove Test Group")).to_be_visible(timeout=5000)

        # Click the Delete button on the group header (not the per-device Remove buttons)
        group_panel = page.locator('div.group-panel[data-group-id="' + group_id + '"]')
        remove_btn = group_panel.locator(".group-actions button", has_text="Delete")
        remove_btn.click()

        # Confirm the modal
        confirm_modal = page.locator(".modal-overlay")
        expect(confirm_modal).to_be_visible(timeout=3000)
        confirm_modal.locator("button", has_text="Confirm").click()
        page.wait_for_load_state("networkidle")

        # Group should be gone
        expect(page.locator("strong", has_text="Remove Test Group")).to_have_count(0)

        # Both devices should still exist (in the ungrouped section)
        expect(page.locator('[data-device-id="grp-rm-001"]').first).to_be_visible(timeout=3000)
        expect(page.locator('[data-device-id="grp-rm-002"]').first).to_be_visible(timeout=3000)

    def test_device_remove_ungroups_single_device(self, page: Page, api, ws_url, e2e_server):
        """Clicking Remove on a device inside a group should ungroup only that device."""
        # Register and adopt two devices
        self._register_and_adopt(api, ws_url, "grp-rm-003", "Device C")
        self._register_and_adopt(api, ws_url, "grp-rm-004", "Device D")

        # Create a group
        resp = api.post("/api/devices/groups/", json={"name": "Single Remove Group"})
        assert resp.status_code == 201, f"Group create failed {resp.status_code}: {resp.text}"
        group_id = resp.json()["id"]

        # Assign both devices to the group
        api.patch("/api/devices/grp-rm-003", json={"group_id": group_id})
        api.patch("/api/devices/grp-rm-004", json={"group_id": group_id})

        # Load the page
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        # Expand the group
        group_panel = page.locator('div.group-panel[data-group-id="' + group_id + '"]')
        expect(group_panel).to_be_visible(timeout=5000)
        group_panel.locator(".group-header").click()

        # Find Device C's row within the group and click its Remove button
        group_body = group_panel.locator(".group-body")
        expect(group_body).to_be_visible(timeout=3000)
        device_row = group_body.locator('tr[data-device-id="grp-rm-003"]')
        expect(device_row).to_be_visible(timeout=3000)
        device_row.locator("button", has_text="Remove").click()

        page.wait_for_load_state("networkidle")

        # Reload to verify
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        # The group should still exist
        expect(page.locator("strong", has_text="Single Remove Group")).to_be_visible(timeout=5000)

        # Expand the group again
        group_panel = page.locator('div.group-panel[data-group-id="' + group_id + '"]')
        group_panel.locator(".group-header").click()
        group_body = group_panel.locator(".group-body")
        expect(group_body).to_be_visible(timeout=3000)

        # Device D should still be in the group
        expect(group_body.locator('tr[data-device-id="grp-rm-004"]')).to_be_visible(timeout=3000)

        # Device C should NOT be in the group (removed)
        expect(group_body.locator('tr[data-device-id="grp-rm-003"]')).to_have_count(0)

        # Device C should still exist on the page (ungrouped)
        expect(page.locator('[data-device-id="grp-rm-003"]').first).to_be_visible(timeout=3000)

    def test_group_remove_blocked_by_schedule(self, page: Page, api, ws_url, e2e_server):
        """Remove button should be disabled when a group has active schedules."""
        self._register_and_adopt(api, ws_url, "grp-sched-001", "Sched Device")

        resp = api.post("/api/devices/groups/", json={"name": "Scheduled Group"})
        assert resp.status_code == 201
        group_id = resp.json()["id"]
        api.patch("/api/devices/grp-sched-001", json={"group_id": group_id})

        # Create an asset and a schedule targeting this group
        assets = api.get("/api/assets")
        if not assets.json():
            api.create_asset("grp-block-test.mp4")
            assets = api.get("/api/assets")
            if not assets.json():
                pytest.skip("Could not create test asset")

        sched_resp = api.post("/api/schedules", json={
            "name": "Blocking Schedule",
            "group_id": group_id,
            "asset_id": assets.json()[0]["id"],
            "start_time": "08:00",
            "end_time": "20:00",
        })
        assert sched_resp.status_code == 201

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        group_panel = page.locator('div.group-panel[data-group-id="' + group_id + '"]')
        expect(group_panel).to_be_visible(timeout=5000)

        # The Delete button should be disabled
        remove_btn = group_panel.locator(".group-actions button:has-text('Delete')")
        expect(remove_btn).to_be_visible(timeout=3000)
        expect(remove_btn).to_be_disabled()

        # Tooltip should mention schedule(s)
        tooltip = remove_btn.locator(".tooltip")
        expect(tooltip).to_contain_text("schedule")

    def test_group_remove_enabled_after_schedule_deleted(self, page: Page, api, ws_url, e2e_server):
        """Remove button should become enabled after the schedule referencing the group is deleted."""
        self._register_and_adopt(api, ws_url, "grp-unsched-001", "Unsched Device")

        resp = api.post("/api/devices/groups/", json={"name": "Was Scheduled Group"})
        assert resp.status_code == 201
        group_id = resp.json()["id"]
        api.patch("/api/devices/grp-unsched-001", json={"group_id": group_id})

        assets = api.get("/api/assets")
        if not assets.json():
            api.create_asset("grp-unblock-test.mp4")
            assets = api.get("/api/assets")
            if not assets.json():
                pytest.skip("Could not create test asset")

        sched_resp = api.post("/api/schedules", json={
            "name": "Temp Schedule",
            "group_id": group_id,
            "asset_id": assets.json()[0]["id"],
            "start_time": "08:00",
            "end_time": "20:00",
        })
        assert sched_resp.status_code == 201
        sched_id = sched_resp.json()["id"]

        # Delete the schedule
        del_resp = api.delete(f"/api/schedules/{sched_id}")
        assert del_resp.status_code == 200

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        group_panel = page.locator('div.group-panel[data-group-id="' + group_id + '"]')
        expect(group_panel).to_be_visible(timeout=5000)

        # The Delete button should now be enabled
        remove_btn = group_panel.locator(".group-actions button", has_text="Delete")
        expect(remove_btn).to_be_visible(timeout=3000)
        expect(remove_btn).to_be_enabled()


class TestGroupRemoveInPlaceMove:
    """Regression tests for #146 — the row should move between sections without
    a full page reload after a group assignment changes."""

    def _register_and_adopt(self, api, ws_url, device_id, name=None):
        async def register():
            async with FakeDevice(device_id, ws_url, device_name=name) as dev:
                await dev.send_status()

        run_async(register())
        # Properly adopt with a profile so the device leaves PENDING and is
        # eligible to appear in the Ungrouped section (per cms/ui.py).
        profiles = api.get("/api/profiles").json()
        profile_id = profiles[0]["id"] if profiles else None
        body = {"name": name or device_id}
        if profile_id:
            body["profile_id"] = profile_id
        api.post(f"/api/devices/{device_id}/adopt", json=body)

    def test_remove_moves_row_to_ungrouped_without_reload(self, page: Page, api, ws_url, e2e_server):
        """Clicking Remove inside a group should move the row to the Ungrouped
        section in place (no page navigation), update the group count, and reveal
        the 'no devices' placeholder when the group becomes empty."""
        self._register_and_adopt(api, ws_url, "inplace-001", "Solo Device")

        resp = api.post("/api/devices/groups/", json={"name": "Inplace Move Group"})
        assert resp.status_code == 201
        group_id = resp.json()["id"]
        api.patch("/api/devices/inplace-001", json={"group_id": group_id})

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        # Capture the URL so we can assert later that we never navigated.
        nav_url_before = page.url

        # Sanity: row starts in the group tbody, not in the ungrouped tbody.
        group_tbody = page.locator(f'tbody[data-group-tbody="{group_id}"]')
        ungrouped_tbody = page.locator('tbody[data-group-tbody="ungrouped"]')
        expect(group_tbody.locator('tr[data-device-id="inplace-001"]')).to_have_count(1)
        expect(ungrouped_tbody.locator('tr[data-device-id="inplace-001"]')).to_have_count(0)

        # Expand and click Remove.
        group_panel = page.locator(f'div.group-panel[data-group-id="{group_id}"]')
        group_panel.locator(".group-header").click()
        device_row = group_panel.locator('tr[data-device-id="inplace-001"]')
        expect(device_row).to_be_visible(timeout=3000)
        device_row.locator("button", has_text="Remove").click()

        # Wait for the in-place move (no reload) — row should appear in the
        # ungrouped tbody and disappear from the group tbody.
        expect(ungrouped_tbody.locator('tr[data-device-id="inplace-001"]')).to_have_count(1, timeout=3000)
        expect(group_tbody.locator('tr[data-device-id="inplace-001"]')).to_have_count(0)

        # The Ungrouped section should now be visible.
        expect(page.locator('#ungrouped-section')).to_be_visible()

        # The group's device count badge should decrement to 0.
        count_badge = page.locator(f'[data-group-count="{group_id}"]')
        expect(count_badge).to_have_text("0 devices")

        # The empty-state placeholder for the group should be visible.
        expect(page.locator(f'[data-group-empty="{group_id}"]')).to_be_visible()

        # The page must not have navigated — confirms it was an in-place update.
        assert page.url == nav_url_before, (
            "Page should not reload when removing a device from a group"
        )

        # The Remove button should be gone from the moved row (now ungrouped).
        moved_row = ungrouped_tbody.locator('tr[data-device-id="inplace-001"]')
        expect(moved_row.locator("button", has_text="Remove")).to_have_count(0)

    def test_assign_to_group_moves_row_from_ungrouped_in_place(self, page: Page, api, ws_url, e2e_server):
        """Assigning an ungrouped device to a group via the inline dropdown
        should move its row out of the Ungrouped section without a reload."""
        self._register_and_adopt(api, ws_url, "inplace-002", "Migrating Device")
        # Adopt a second device into the group so the group panel starts non-empty.
        self._register_and_adopt(api, ws_url, "inplace-003", "Anchor Device")

        resp = api.post("/api/devices/groups/", json={"name": "Inplace Assign Group"})
        assert resp.status_code == 201
        group_id = resp.json()["id"]
        api.patch("/api/devices/inplace-003", json={"group_id": group_id})

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        nav_url_before = page.url

        ungrouped_tbody = page.locator('tbody[data-group-tbody="ungrouped"]')
        group_tbody = page.locator(f'tbody[data-group-tbody="{group_id}"]')
        expect(ungrouped_tbody.locator('tr[data-device-id="inplace-002"]')).to_have_count(1)

        # Use the inline group <select> in the ungrouped row to assign the group.
        ungrouped_row = ungrouped_tbody.locator('tr[data-device-id="inplace-002"]')
        ungrouped_row.locator('select[data-device-group-select]').select_option(group_id)

        # Row should appear in the group tbody and vanish from ungrouped.
        expect(group_tbody.locator('tr[data-device-id="inplace-002"]')).to_have_count(1, timeout=3000)
        expect(ungrouped_tbody.locator('tr[data-device-id="inplace-002"]')).to_have_count(0)

        # Group device-count badge should bump from 1 → 2.
        expect(page.locator(f'[data-group-count="{group_id}"]')).to_have_text("2 devices")

        # No reload happened.
        assert page.url == nav_url_before

        # The moved row should now have a Remove button.
        moved_row = group_tbody.locator('tr[data-device-id="inplace-002"]')
        expect(moved_row.locator("button", has_text="Remove")).to_have_count(1)
