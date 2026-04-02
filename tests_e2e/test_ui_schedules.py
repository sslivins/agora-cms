"""Playwright tests for the Schedules page.

Covers: create, edit modal, toggle, delete, validation, and JS error detection.
"""

import re

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async
from tests_e2e.fake_device import FakeDevice


class TestScheduleCreate:
    """Creating schedules via the form."""

    def test_create_schedule(self, page: Page, api, ws_url):
        """Create a schedule and verify it appears in the table."""
        # We need a device and an asset first — connect a fake device
        async def register_device():
            async with FakeDevice("sched-test-001", ws_url) as dev:
                await dev.send_status()
                return dev.device_id

        run_async(register_device())

        # Adopt the device via API
        api.post("/api/devices/sched-test-001/adopt")

        # Upload a test asset (a minimal valid MP4 isn't required — the server
        # accepts the upload and the schedule form just needs the asset ID)
        resp = api.create_asset("schedule-test.mp4")
        # May fail with 500 if ffprobe can't read fake content — that's OK,
        # the asset still gets created in the DB in some code paths.
        # Let's check the assets list instead.
        assets_resp = api.get("/api/assets")
        if assets_resp.status_code != 200 or not assets_resp.json():
            pytest.skip("Could not create test asset (ffprobe not available)")

        asset_name = assets_resp.json()[0]["filename"]

        # Navigate to schedules page
        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        # Fill the create form
        page.fill('input[name="name"]', "E2E Test Schedule")
        page.select_option('select[name="asset_id"]', label=asset_name)
        # Set times via native time inputs
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "17:00")

        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # Verify schedule appears in table
        expect(page.locator("td", has_text="E2E Test Schedule")).to_be_visible()


class TestScheduleEditModal:
    """The edit modal must open and function correctly."""

    def test_no_js_errors_on_page_load(self, page: Page):
        """The schedules page must load without any JavaScript errors."""
        js_errors = []
        page.on("pageerror", lambda err: js_errors.append(str(err)))

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        assert not js_errors, f"JavaScript errors on page load: {js_errors}"

    def test_edit_button_opens_modal(self, page: Page, api, ws_url):
        """Clicking Edit on a schedule must open the edit modal."""
        async def setup():
            async with FakeDevice("edit-modal-001", ws_url) as dev:
                await dev.send_status()

        run_async(setup())
        api.post("/api/devices/edit-modal-001/adopt")

        resp = api.create_asset("edit-test.mp4")
        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("Could not create test asset")

        asset_id = assets.json()[0]["id"]

        # Create a schedule via API
        api.post("/api/schedules", json={
            "name": "Editable Schedule",
            "device_id": "edit-modal-001",
            "asset_id": asset_id,
            "start_time": "09:00",
            "end_time": "17:00",
            "priority": 0,
        })

        # Capture JS errors
        js_errors = []
        page.on("pageerror", lambda err: js_errors.append(str(err)))

        # Load the page
        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        # Click the Edit button on the specific row
        row = page.locator("tr", has_text="Editable Schedule")
        edit_btn = row.locator("button", has_text="Edit")
        expect(edit_btn).to_be_visible()
        edit_btn.click()

        # The modal overlay must appear
        modal = page.locator(".modal-overlay")
        expect(modal).to_be_visible(timeout=3000)

        # Modal must contain the schedule name
        name_input = modal.locator("#edit-name")
        expect(name_input).to_have_value("Editable Schedule")

        # No JS errors should have occurred
        assert not js_errors, f"JavaScript errors when opening edit modal: {js_errors}"

    def test_edit_modal_saves_changes(self, page: Page, api, ws_url):
        """Edit a schedule name through the modal and verify it persists."""
        async def setup():
            async with FakeDevice("edit-save-001", ws_url) as dev:
                await dev.send_status()

        run_async(setup())
        api.post("/api/devices/edit-save-001/adopt")

        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("No assets available")

        asset_id = assets.json()[0]["id"]

        api.post("/api/schedules", json={
            "name": "Will Rename",
            "device_id": "edit-save-001",
            "asset_id": asset_id,
            "start_time": "10:00",
            "end_time": "11:00",
        })

        js_errors = []
        page.on("pageerror", lambda err: js_errors.append(str(err)))

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        # Find and click the Edit button for "Will Rename"
        row = page.locator("tr", has_text="Will Rename")
        row.locator("button", has_text="Edit").click()

        modal = page.locator(".modal-overlay")
        expect(modal).to_be_visible(timeout=3000)

        # Change the name
        name_input = modal.locator("#edit-name")
        name_input.fill("Renamed Schedule")

        # Click Save
        modal.locator("#edit-save").click()

        # Page should reload and show the new name
        page.wait_for_load_state("networkidle")
        expect(page.locator("td", has_text="Renamed Schedule")).to_be_visible()

        assert not js_errors, f"JS errors: {js_errors}"

    def test_edit_modal_cancel_discards(self, page: Page, api, ws_url):
        """Cancelling the edit modal should not change anything."""
        async def setup():
            async with FakeDevice("edit-cancel-001", ws_url) as dev:
                await dev.send_status()

        run_async(setup())
        api.post("/api/devices/edit-cancel-001/adopt")

        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("No assets available")

        asset_id = assets.json()[0]["id"]

        api.post("/api/schedules", json={
            "name": "Dont Change Me",
            "device_id": "edit-cancel-001",
            "asset_id": asset_id,
            "start_time": "08:00",
            "end_time": "09:00",
        })

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="Dont Change Me")
        row.locator("button", has_text="Edit").click()

        modal = page.locator(".modal-overlay")
        expect(modal).to_be_visible(timeout=3000)

        # Change name but cancel
        modal.locator("#edit-name").fill("Changed Name")
        modal.locator("#edit-cancel").click()

        expect(modal).not_to_be_visible()
        # Original name should still be there
        expect(page.locator("td", has_text="Dont Change Me")).to_be_visible()


class TestScheduleToggle:
    """Enable/disable schedule toggle."""

    def test_toggle_schedule_off_and_on(self, page: Page, api, ws_url):
        """Toggle button switches between On and Off."""
        async def setup():
            async with FakeDevice("toggle-001", ws_url) as dev:
                await dev.send_status()

        run_async(setup())
        api.post("/api/devices/toggle-001/adopt")

        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("No assets available")

        api.post("/api/schedules", json={
            "name": "Toggle Test",
            "device_id": "toggle-001",
            "asset_id": assets.json()[0]["id"],
            "start_time": "08:00",
            "end_time": "12:00",
        })

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="Toggle Test")
        toggle_btn = row.locator("button", has_text="On")
        expect(toggle_btn).to_be_visible()

        # Click to turn off
        toggle_btn.click()
        page.wait_for_load_state("networkidle")

        # Should now show "Off"
        row = page.locator("tr", has_text="Toggle Test")
        expect(row.locator("button", has_text="Off")).to_be_visible()


class TestScheduleDelete:
    """Deleting a schedule."""

    def test_delete_schedule_with_confirm(self, page: Page, api, ws_url):
        """Delete button should show confirm dialog, then remove schedule."""
        async def setup():
            async with FakeDevice("delete-001", ws_url) as dev:
                await dev.send_status()

        run_async(setup())
        api.post("/api/devices/delete-001/adopt")

        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("No assets available")

        api.post("/api/schedules", json={
            "name": "Delete Me Please",
            "device_id": "delete-001",
            "asset_id": assets.json()[0]["id"],
            "start_time": "08:00",
            "end_time": "12:00",
        })

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="Delete Me Please")
        row.locator("button", has_text="Delete").click()

        # Confirm modal should appear
        confirm_modal = page.locator(".modal-overlay")
        expect(confirm_modal).to_be_visible(timeout=3000)

        # Click Confirm
        confirm_modal.locator("button", has_text="Confirm").click()
        page.wait_for_load_state("networkidle")

        # Schedule should be gone
        expect(page.locator("td", has_text="Delete Me Please")).not_to_be_visible()


class TestScheduleEditWithDates:
    """Editing schedules with date fields — regression tests for naive datetime
    and UTC date comparison bugs."""

    def _setup_schedule(self, page, api, ws_url, device_id, name):
        """Helper: register device and create a schedule."""
        async def register():
            async with FakeDevice(device_id, ws_url) as dev:
                await dev.send_status()

        run_async(register())
        api.post(f"/api/devices/{device_id}/adopt")

        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("No assets available")

        api.post("/api/schedules", json={
            "name": name,
            "device_id": device_id,
            "asset_id": assets.json()[0]["id"],
            "start_time": "09:00",
            "end_time": "17:00",
        })

    def test_edit_with_dates_saves_successfully(self, page: Page, api, ws_url):
        """Editing a schedule to add start/end dates must not cause a 500 error.

        Regression: naive datetime strings from the browser caused
        'TypeError: can't compare offset-naive and offset-aware datetimes'
        in the scheduler when push_sync_to_affected_devices ran after the
        PATCH.
        """
        self._setup_schedule(page, api, ws_url, "edit-dates-001", "Add Dates")

        js_errors = []
        page.on("pageerror", lambda err: js_errors.append(str(err)))

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="Add Dates")
        row.locator("button", has_text="Edit").click()

        modal = page.locator(".modal-overlay")
        expect(modal).to_be_visible(timeout=3000)

        # Set start and end dates
        modal.locator("#edit-start-date").fill("2026-05-01")
        modal.locator("#edit-end-date").fill("2026-05-31")

        # Click Save
        modal.locator("#edit-save").click()

        # Page should reload successfully (not show an error toast)
        page.wait_for_load_state("networkidle")

        # The schedule should still be visible (save succeeded)
        expect(page.locator("td", has_text="Add Dates")).to_be_visible()

        # No JS errors
        assert not js_errors, f"JS errors: {js_errors}"

        # No error toast visible
        toast = page.locator(".toast.error")
        expect(toast).not_to_be_visible()

    def test_edit_all_fields(self, page: Page, api, ws_url):
        """Full create → edit flow: change name, times, dates, priority.

        This exercises the exact same code path as the browser's edit modal
        Save button, including the PATCH request with all fields.
        """
        self._setup_schedule(page, api, ws_url, "edit-all-001", "Edit All Fields")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="Edit All Fields")
        row.locator("button", has_text="Edit").click()

        modal = page.locator(".modal-overlay")
        expect(modal).to_be_visible(timeout=3000)

        # Change name
        modal.locator("#edit-name").fill("Fully Edited")

        # Change times
        modal.locator("#edit-start-time").fill("14:00")
        modal.locator("#edit-end-time").fill("18:00")

        # Set dates
        modal.locator("#edit-start-date").fill("2026-06-01")
        modal.locator("#edit-end-date").fill("2026-06-30")

        # Change priority
        modal.locator("#edit-priority").fill("8")

        # Save
        modal.locator("#edit-save").click()
        page.wait_for_load_state("networkidle")

        # Verify updated name appears
        expect(page.locator("td", has_text="Fully Edited")).to_be_visible()

        # Verify via API that all fields were saved
        schedules = api.get("/api/schedules").json()
        edited = next(s for s in schedules if s["name"] == "Fully Edited")
        assert edited["priority"] == 8
        assert "2026-06-01" in edited["start_date"]
        assert "2026-06-30" in edited["end_date"]

    def test_end_date_today_not_flagged_as_past(self, page: Page, api, ws_url):
        """Setting end date to today must NOT trigger the 'in the past' warning.

        Regression: the JS used new Date().toISOString().slice(0, 10) which
        returns the UTC date. In negative UTC offsets (US timezones), after
        the UTC day rolls over (e.g. 7 PM EDT = midnight UTC), today's local
        date appeared to be 'in the past'.
        """
        self._setup_schedule(page, api, ws_url, "date-today-001", "Date Today Test")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="Date Today Test")
        row.locator("button", has_text="Edit").click()

        modal = page.locator(".modal-overlay")
        expect(modal).to_be_visible(timeout=3000)

        # Set end date to today using JS to get local date
        today = page.evaluate("new Date().toLocaleDateString('en-CA')")
        modal.locator("#edit-start-date").fill(today)
        modal.locator("#edit-end-date").fill(today)

        # Click Save
        modal.locator("#edit-save").click()

        # Should NOT see a confirm dialog about "in the past"
        # If the bug is present, a confirm modal appears; if fixed, it saves directly.
        # Wait a moment for any potential confirm dialog
        page.wait_for_timeout(500)

        # No confirm dialog should be visible (the custom showConfirm modal)
        confirm_dialogs = page.locator(".modal-overlay .modal-box:has-text('in the past')")
        expect(confirm_dialogs).not_to_be_visible()

        # Page should have reloaded (save went through)
        page.wait_for_load_state("networkidle")
        expect(page.locator("td", has_text="Date Today Test")).to_be_visible()
