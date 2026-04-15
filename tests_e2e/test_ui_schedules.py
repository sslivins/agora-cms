"""Playwright tests for the Schedules page.

Covers: create, edit modal, toggle, delete, validation, and JS error detection.
"""

import re
from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async
from tests_e2e.fake_device import FakeDevice


def _ensure_device_and_asset(api, ws_url, device_id):
    """Register + adopt a device, create a group, and ensure at least one asset exists.

    Returns (asset_filename, group_id) for use in the schedule form.
    """
    async def register():
        async with FakeDevice(device_id, ws_url) as dev:
            await dev.send_status()

    run_async(register())
    api.post(f"/api/devices/{device_id}/adopt")

    group_resp = api.post("/api/devices/groups/", json={"name": f"Group-{device_id}"})
    group_id = group_resp.json()["id"]
    api.patch(f"/api/devices/{device_id}", json={"group_id": group_id})

    assets = api.get("/api/assets")
    if not assets.json():
        api.create_asset("e2e-shared-test.mp4")
        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("Could not create test asset (ffprobe not available)")

    return assets.json()[0]["filename"], group_id


def _fill_create_form(page, name, asset_name, group_id, start, end):
    """Fill the schedule create form, explicitly targeting a specific group."""
    page.fill('input[name="name"]', name)
    page.select_option('select[name="asset_id"]', label=asset_name)
    page.select_option('select[name="group_id"]', value=group_id)
    page.fill('input[name="start_time"]', start)
    page.fill('input[name="end_time"]', end)


class TestScheduleCreate:
    """Creating schedules via the form."""

    def test_create_schedule_appears_in_active_table(self, page: Page, api, ws_url):
        """Create a schedule and verify it appears in the Active Schedules table."""
        asset_name, group_id = _ensure_device_and_asset(api, ws_url, "sched-test-001")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        _fill_create_form(page, "E2E Active Check", asset_name, group_id, "09:00", "17:00")

        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # Must appear in the Active Schedules table (the second card)
        active_card = page.locator(".card", has_text="Active Schedules")
        expect(active_card.locator("td", has_text="E2E Active Check")).to_be_visible()

    def test_create_schedule_exists_in_api(self, page: Page, api, ws_url):
        """After form submit, the schedule must exist in the REST API."""
        asset_name, group_id = _ensure_device_and_asset(api, ws_url, "sched-api-001")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        _fill_create_form(page, "E2E API Verify", asset_name, group_id, "10:00", "11:00")

        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # Verify through the API — NOT just the DOM
        schedules = api.get("/api/schedules").json()
        names = [s["name"] for s in schedules]
        assert "E2E API Verify" in names, (
            f"Schedule not found in API after form submit. Got: {names}"
        )

    def test_create_upcoming_schedule_on_dashboard(self, page: Page, api, ws_url):
        """A schedule starting later today must appear in the dashboard Coming Up panel.

        Regression coverage: if the form silently fails (e.g. GET instead of
        POST), the schedule won't exist and won't appear on the dashboard.
        """
        asset_name, group_id = _ensure_device_and_asset(api, ws_url, "sched-dash-001")

        # Pick a start time 2 hours from now (UTC) so it's "upcoming today"
        now_utc = datetime.now(timezone.utc)
        start = (now_utc + timedelta(hours=2)).strftime("%H:%M")
        end = (now_utc + timedelta(hours=3)).strftime("%H:%M")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        _fill_create_form(page, "E2E Dashboard Check", asset_name, group_id, start, end)

        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # Confirm it's in Active Schedules first
        active_card = page.locator(".card", has_text="Active Schedules")
        expect(active_card.locator("td", has_text="E2E Dashboard Check")).to_be_visible()

        # Now navigate to the dashboard
        page.goto("/")
        page.wait_for_load_state("domcontentloaded")

        # The "Coming Up" card should list our schedule
        coming_up_card = page.locator(".card", has_text="Coming Up")
        expect(
            coming_up_card.locator("td", has_text="E2E Dashboard Check")
        ).to_be_visible(timeout=5000)

    def test_create_form_uses_post_not_get(self, page: Page, api, ws_url):
        """Form submission must send a POST via JS, not a native GET.

        Regression: ``async function createSchedule`` returned a Promise
        (truthy) from ``onsubmit="return createSchedule(this)"``, so the
        browser fell through to the default GET submission.  The JS POST
        could race the navigation and sometimes succeed, hiding the bug.
        """
        asset_name, group_id = _ensure_device_and_asset(api, ws_url, "create-post-001")

        # Track all requests to /schedules
        requests_log = []
        page.on("request", lambda req: requests_log.append(req)
                if "/schedules" in req.url else None)

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        _fill_create_form(page, "POST Not GET Test", asset_name, group_id, "08:00", "18:00")

        # Clear log to only capture the submit request
        requests_log.clear()

        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # There should be a POST to /api/schedules (the JS fetch)
        post_reqs = [r for r in requests_log
                     if r.method == "POST" and "/api/schedules" in r.url]
        assert post_reqs, "Expected a POST to /api/schedules but none was sent"

        # There must NOT be a GET to /schedules with form query params
        bad_gets = [r for r in requests_log
                    if r.method == "GET" and "name=" in r.url]
        assert not bad_gets, (
            f"Form fell through to native GET submission: {bad_gets[0].url}"
        )

        # Verify through both DOM and API
        expect(page.locator("td", has_text="POST Not GET Test")).to_be_visible()

        schedules = api.get("/api/schedules").json()
        assert any(s["name"] == "POST Not GET Test" for s in schedules), (
            "Schedule created via form not found in API"
        )


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

        group_resp = api.post("/api/devices/groups/", json={"name": "Group-edit-modal-001"})
        group_id = group_resp.json()["id"]
        api.patch("/api/devices/edit-modal-001", json={"group_id": group_id})

        resp = api.create_asset("edit-test.mp4")
        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("Could not create test asset")

        asset_id = assets.json()[0]["id"]

        # Create a schedule via API
        api.post("/api/schedules", json={
            "name": "Editable Schedule",
            "group_id": group_id,
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

        group_resp = api.post("/api/devices/groups/", json={"name": "Group-edit-save-001"})
        group_id = group_resp.json()["id"]
        api.patch("/api/devices/edit-save-001", json={"group_id": group_id})

        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("No assets available")

        asset_id = assets.json()[0]["id"]

        api.post("/api/schedules", json={
            "name": "Will Rename",
            "group_id": group_id,
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

        group_resp = api.post("/api/devices/groups/", json={"name": "Group-edit-cancel-001"})
        group_id = group_resp.json()["id"]
        api.patch("/api/devices/edit-cancel-001", json={"group_id": group_id})

        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("No assets available")

        asset_id = assets.json()[0]["id"]

        api.post("/api/schedules", json={
            "name": "Dont Change Me",
            "group_id": group_id,
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

        group_resp = api.post("/api/devices/groups/", json={"name": "Group-toggle-001"})
        group_id = group_resp.json()["id"]
        api.patch("/api/devices/toggle-001", json={"group_id": group_id})

        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("No assets available")

        api.post("/api/schedules", json={
            "name": "Toggle Test",
            "group_id": group_id,
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

        group_resp = api.post("/api/devices/groups/", json={"name": "Group-delete-001"})
        group_id = group_resp.json()["id"]
        api.patch("/api/devices/delete-001", json={"group_id": group_id})

        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("No assets available")

        api.post("/api/schedules", json={
            "name": "Delete Me Please",
            "group_id": group_id,
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
        """Helper: register device, create group, and create a schedule."""
        async def register():
            async with FakeDevice(device_id, ws_url) as dev:
                await dev.send_status()

        run_async(register())
        api.post(f"/api/devices/{device_id}/adopt")

        group_resp = api.post("/api/devices/groups/", json={"name": f"Group-{device_id}"})
        group_id = group_resp.json()["id"]
        api.patch(f"/api/devices/{device_id}", json={"group_id": group_id})

        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("No assets available")

        api.post("/api/schedules", json={
            "name": name,
            "group_id": group_id,
            "asset_id": assets.json()[0]["id"],
            "start_time": "09:00",
            "end_time": "23:59",
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


class TestScheduleDescriptionColumn:
    """The Schedule column must show human-readable descriptions."""

    def test_every_day_schedule_shows_every_day(self, page: Page, api, ws_url):
        """A schedule with all days and no date range should say 'Every day'."""
        asset_name, group_id = _ensure_device_and_asset(api, ws_url, "desc-col-001")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        _fill_create_form(page, "Desc Every Day", asset_name, group_id, "09:00", "17:00")
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # Find the schedule row and check the description cell
        row = page.locator("tr", has_text="Desc Every Day")
        desc_cell = row.locator("td.schedule-desc")
        text = desc_cell.text_content()
        assert "Every day" in text, f"Expected 'Every day' in description, got: {text}"
        assert "AM" in text or "PM" in text, f"Expected time in description, got: {text}"

    def test_one_shot_schedule_shows_once(self, page: Page, api, ws_url):
        """A schedule with same start/end date should say 'Once on ...'."""
        asset_name, group_id = _ensure_device_and_asset(api, ws_url, "desc-col-002")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        _fill_create_form(page, "Desc One Shot", asset_name, group_id, "14:00", "16:00")
        # Set both dates to the same future date
        page.fill('input[name="start_date"]', "2027-06-15")
        page.fill('input[name="end_date"]', "2027-06-15")
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        row = page.locator("tr", has_text="Desc One Shot")
        desc_cell = row.locator("td.schedule-desc")
        text = desc_cell.text_content()
        assert "Once on" in text, f"Expected 'Once on' in description, got: {text}"

    def test_weekday_schedule_shows_weekdays(self, page: Page, api, ws_url):
        """A schedule with Mon-Fri should say 'Weekdays'."""
        asset_name, group_id = _ensure_device_and_asset(api, ws_url, "desc-col-003")

        # Create via API with specific days
        assets = api.get("/api/assets").json()
        asset_id = assets[0]["id"]
        api.post("/api/schedules", json={
            "name": "Desc Weekdays",
            "asset_id": asset_id,
            "group_id": group_id,
            "start_time": "08:00:00",
            "end_time": "17:00:00",
            "days_of_week": [1, 2, 3, 4, 5],
            "priority": 0,
        })

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="Desc Weekdays")
        desc_cell = row.locator("td.schedule-desc")
        text = desc_cell.text_content()
        assert "Weekdays" in text, f"Expected 'Weekdays' in description, got: {text}"


class TestScheduleEditSummaryBanner:
    """The edit modal must show a live schedule summary banner."""

    def test_edit_modal_shows_summary(self, page: Page, api, ws_url):
        """Opening the edit modal must display the schedule summary banner."""
        asset_name, group_id = _ensure_device_and_asset(api, ws_url, "edit-sum-001")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        _fill_create_form(page, "Edit Summary Test", asset_name, group_id, "10:00", "12:00")
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        # Click edit on the schedule
        row = page.locator("tr", has_text="Edit Summary Test")
        row.locator("button", has_text="Edit").click()

        # The edit modal summary should be visible
        summary = page.locator("#edit-schedule-summary")
        expect(summary).to_be_visible()
        text = summary.text_content()
        assert "will play" in text, f"Expected summary text, got: {text}"

    def test_edit_modal_summary_updates_on_time_change(self, page: Page, api, ws_url):
        """Changing times in the edit modal must update the summary banner."""
        asset_name, group_id = _ensure_device_and_asset(api, ws_url, "edit-sum-002")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        _fill_create_form(page, "Edit Summary Update", asset_name, group_id, "10:00", "12:00")
        page.click('button[type="submit"]')
        page.wait_for_load_state("networkidle")

        row = page.locator("tr", has_text="Edit Summary Update")
        row.locator("button", has_text="Edit").click()

        summary = page.locator("#edit-schedule-summary")
        expect(summary).to_be_visible()
        initial_text = summary.text_content()

        # Change the end time
        page.fill("#edit-end-time", "18:00")
        page.locator("#edit-end-time").dispatch_event("change")

        updated_text = summary.text_content()
        assert updated_text != initial_text, (
            f"Summary did not update after time change. Still: {initial_text}"
        )
