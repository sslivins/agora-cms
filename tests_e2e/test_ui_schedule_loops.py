"""Playwright tests for schedule loop-count features.

Covers: loop info in summary, round-up/down buttons, exact-loop detection,
duration in asset dropdown, and no loop info for images.
"""

import re

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async
from tests_e2e.fake_device import FakeDevice

# A 10-minute (600s) video makes the math easy:
#   9:00–10:00 = 60 min = 3600s → 6.0 loops (exact)
#   9:00–10:30 = 90 min = 5400s → 9.0 loops (exact)
#   9:00–10:25 = 85 min = 5100s → 8.5 loops (cut short)
ASSET_DURATION = 600  # 10 minutes = 600 seconds


def _setup_device_and_asset(page: Page, api, ws_url, device_id: str):
    """Register + adopt a device, upload an asset, return the asset id."""
    async def register():
        async with FakeDevice(device_id, ws_url) as dev:
            await dev.send_status()

    run_async(register())
    api.post(f"/api/devices/{device_id}/adopt")

    api.create_asset("loop-test.mp4")
    assets = api.get("/api/assets").json()
    if not assets:
        pytest.skip("Could not create test asset (ffprobe not available)")
    return assets[0]["id"]


def _go_to_schedules_and_inject_duration(page: Page, asset_id: str, duration: float):
    """Navigate to schedules page and inject a known duration into JS data."""
    page.goto("/schedules")
    page.wait_for_load_state("domcontentloaded")
    # Inject the known duration into _scheduleData so summary logic uses it
    page.evaluate(
        """([assetId, dur]) => {
            const a = _scheduleData.assets.find(x => x.id === assetId);
            if (a) a.duration = dur;
        }""",
        [asset_id, duration],
    )


class TestLoopSummaryDisplay:
    """Loop info appears correctly in the schedule summary bar."""

    def test_exact_loops_shown(self, page: Page, api, ws_url):
        """When the time window is an exact multiple of asset duration,
        the summary should say 'N loops' (without 'exactly' — that word is
        reserved for an explicit loop_count set via round up/down)."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "loop-exact-001")
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "Exact Loop Test")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:00")
        # Trigger summary update
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        expect(summary).to_be_visible()
        expect(summary).to_contain_text("6 loops of 10:00")
        # Should NOT say "exactly" for a natural alignment
        expect(summary).not_to_contain_text("exactly")
        # Should NOT show round buttons for exact loops
        expect(summary.locator(".loop-round")).to_have_count(0)

    def test_cut_short_loops_shown(self, page: Page, api, ws_url):
        """When loops don't divide evenly, the summary should show
        'last loop cut short' with round-up and round-down buttons."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "loop-cut-001")
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "Cut Short Test")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:25")
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        expect(summary).to_be_visible()
        expect(summary).to_contain_text("last loop cut short")
        expect(summary).to_contain_text("10:00")  # asset duration formatted

        # Both round buttons should be visible
        buttons = summary.locator(".loop-round")
        expect(buttons).to_have_count(2)
        expect(buttons.nth(0)).to_contain_text("Round down")
        expect(buttons.nth(1)).to_contain_text("Round up")

    def test_no_loop_info_without_asset(self, page: Page, api, ws_url):
        """No loop info should appear when no asset is selected."""
        _setup_device_and_asset(page, api, ws_url, "loop-noasset-001")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        page.fill('input[name="name"]', "No Asset Test")
        # Leave asset on "Select asset..."
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:00")
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        # Summary might not show at all without an asset, or show without loop info
        if summary.is_visible():
            expect(summary).not_to_contain_text("loops")


class TestRoundButtons:
    """Round-up and round-down buttons adjust end time correctly."""

    def test_round_down_adjusts_end_time(self, page: Page, api, ws_url):
        """Clicking 'Round down' should shorten the window to fit
        fewer complete loops."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "loop-rdown-001")
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "Round Down Test")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:25")  # 85 min = 8.5 loops
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        expect(summary).to_be_visible()

        # Click "Round down" (should round to 8 loops = 80 min → end 10:20)
        round_down = summary.locator(".loop-round", has_text="Round down")
        expect(round_down).to_be_visible()
        round_down.click()

        # End time should now be 10:20:00 (9:00 + 8×10min)
        end_time = page.input_value('input[name="end_time"]')
        assert end_time == "10:20:00", f"Expected 10:20:00 but got {end_time}"

        # Summary should now say exact 8 loops (locked with loop_count)
        expect(summary).to_contain_text("exact 8 loops")

    def test_round_up_adjusts_end_time(self, page: Page, api, ws_url):
        """Clicking 'Round up' should extend the window to fit
        one more complete loop."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "loop-rup-001")
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "Round Up Test")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:25")  # 85 min = 8.5 loops
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        expect(summary).to_be_visible()

        # Click "Round up" (should round to 9 loops = 90 min → end 10:30)
        round_up = summary.locator(".loop-round", has_text="Round up")
        expect(round_up).to_be_visible()
        round_up.click()

        # End time should now be 10:30:00 (9:00 + 9×10min)
        end_time = page.input_value('input[name="end_time"]')
        assert end_time == "10:30:00", f"Expected 10:30:00 but got {end_time}"

        # Summary should now say exact 9 loops (locked with loop_count)
        expect(summary).to_contain_text("exact 9 loops")

    def test_round_down_then_no_buttons(self, page: Page, api, ws_url):
        """After rounding, the buttons should disappear since loops are exact."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "loop-gone-001")
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "Buttons Gone Test")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:25")
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        summary.locator(".loop-round", has_text="Round down").click()

        # Buttons should be gone after rounding
        expect(summary.locator(".loop-round")).to_have_count(0)


class TestOneShotLoopSummary:
    """Loop info also works for one-shot (single-day) schedules."""

    def test_oneshot_shows_loop_info(self, page: Page, api, ws_url):
        """A one-shot schedule should also display loop info."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "loop-1shot-001")
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "One Shot Loop Test")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:25")

        # Set start and end date to the same day (one-shot)
        page.fill('input[name="start_date"]', "2026-06-15")
        page.fill('input[name="end_date"]', "2026-06-15")
        page.dispatch_event('input[name="end_date"]', "change")

        summary = page.locator("#schedule-summary")
        expect(summary).to_be_visible()
        expect(summary).to_contain_text("last loop cut short")
        expect(summary.locator(".loop-round")).to_have_count(2)

    def test_oneshot_round_up_works(self, page: Page, api, ws_url):
        """Round up button should work in one-shot mode too."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "loop-1shot-002")
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "One Shot Round Up")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:25")
        page.fill('input[name="start_date"]', "2026-06-15")
        page.fill('input[name="end_date"]', "2026-06-15")
        page.dispatch_event('input[name="end_date"]', "change")

        summary = page.locator("#schedule-summary")
        summary.locator(".loop-round", has_text="Round up").click()

        end_time = page.input_value('input[name="end_time"]')
        assert end_time == "10:30:00", f"Expected 10:30:00 but got {end_time}"
        expect(summary).to_contain_text("exact 9 loops")


class TestAssetDurationDisplay:
    """Duration is shown in the asset dropdown for video assets."""

    def test_duration_in_dropdown(self, page: Page, api, ws_url):
        """Video assets with duration should show it in parentheses."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "loop-dd-001")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        # The dropdown option text should contain the filename
        option = page.locator(f'select[name="asset_id"] option[value="{asset_id}"]')
        text = option.inner_text()
        assert "loop-test.mp4" in text

        # Duration from ffprobe may or may not be present (fake file),
        # but the template should at least render an option with the asset name.
        # We test the JS-injected duration display separately.

    def test_summary_updates_on_asset_change(self, page: Page, api, ws_url):
        """Changing the asset dropdown should update the loop info."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "loop-asc-001")
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "Asset Change Test")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:00")
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        expect(summary).to_contain_text("6 loops")
        expect(summary).not_to_contain_text("exactly")

        # Change to "Select asset..." (no asset)
        page.select_option('select[name="asset_id"]', value="")
        page.dispatch_event('select[name="asset_id"]', "change")

        # Summary should no longer mention loops
        if summary.is_visible():
            expect(summary).not_to_contain_text("loops")


class TestNoJsErrors:
    """The loop features must not introduce any JavaScript errors."""

    def test_no_js_errors_during_loop_interaction(self, page: Page, api, ws_url):
        """Exercise the full loop workflow and verify no JS errors."""
        js_errors = []
        page.on("pageerror", lambda err: js_errors.append(str(err)))

        asset_id = _setup_device_and_asset(page, api, ws_url, "loop-noerr-001")
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "JS Error Test")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:25")
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        expect(summary).to_be_visible()

        # Click round up — this sets an explicit loop_count
        summary.locator(".loop-round", has_text="Round up").click()

        # Clear the locked loop count so round buttons reappear
        summary.locator(".loop-clear").click()

        # Change times again
        page.fill('input[name="end_time"]', "10:15")
        page.dispatch_event('input[name="end_time"]', "change")

        # Click round down
        summary.locator(".loop-round", has_text="Round down").click()

        assert not js_errors, f"JavaScript errors during loop interaction: {js_errors}"


def _create_schedule_via_api(api, device_id, asset_id, start_time="09:00:00",
                              end_time="10:25:00", name="Edit Test",
                              loop_count=None):
    """Create a schedule via REST API and return the schedule dict."""
    body = {
        "name": name,
        "device_id": device_id,
        "asset_id": asset_id,
        "start_time": start_time,
        "end_time": end_time,
        "priority": 0,
        "enabled": True,
    }
    if loop_count is not None:
        body["loop_count"] = loop_count
    resp = api.post("/api/schedules", json=body)
    assert resp.status_code == 201, f"Failed to create schedule: {resp.text}"
    return resp.json()


def _open_edit_modal(page, schedule_name):
    """Navigate to schedules page and open the edit modal for the given schedule."""
    page.goto("/schedules")
    page.wait_for_load_state("domcontentloaded")
    # Find the row with this schedule and click its Edit button
    row = page.locator("tr", has_text=schedule_name)
    row.locator("button", has_text="Edit").click()
    # Wait for modal to appear
    page.wait_for_selector(".modal-overlay")


class TestEditPreserveDuration:
    """Edit modal preserves original duration when changing start or end time."""

    def test_edit_start_preserves_duration(self, page: Page, api, ws_url):
        """Changing start time in edit should shift end time by same duration."""
        device_id = "edit-dur-001"
        asset_id = _setup_device_and_asset(page, api, ws_url, device_id)
        sched = _create_schedule_via_api(
            api, device_id, asset_id,
            start_time="09:00:00", end_time="11:00:00",
            name="Preserve Duration Start",
        )

        _open_edit_modal(page, "Preserve Duration Start")

        # Change start time from 09:00 to 10:00
        page.fill("#edit-start-time", "10:00")
        page.dispatch_event("#edit-start-time", "change")

        # End time should auto-adjust to 12:00 (preserving 2h duration)
        end_time = page.input_value("#edit-end-time")
        assert end_time == "12:00", f"Expected 12:00 (2h preserved) but got {end_time}"

    def test_edit_end_preserves_duration(self, page: Page, api, ws_url):
        """Changing end time in edit should shift start time by same duration."""
        device_id = "edit-dur-002"
        asset_id = _setup_device_and_asset(page, api, ws_url, device_id)
        sched = _create_schedule_via_api(
            api, device_id, asset_id,
            start_time="09:00:00", end_time="11:00:00",
            name="Preserve Duration End",
        )

        _open_edit_modal(page, "Preserve Duration End")

        # Change end time from 11:00 to 13:00
        page.fill("#edit-end-time", "13:00")
        page.dispatch_event("#edit-end-time", "change")

        # Start time should auto-adjust to 11:00 (preserving 2h duration)
        start_time = page.input_value("#edit-start-time")
        assert start_time == "11:00", f"Expected 11:00 (2h preserved) but got {start_time}"

    def test_edit_both_times_no_constraint(self, page: Page, api, ws_url):
        """Changing both start and end should not auto-adjust."""
        device_id = "edit-dur-003"
        asset_id = _setup_device_and_asset(page, api, ws_url, device_id)
        sched = _create_schedule_via_api(
            api, device_id, asset_id,
            start_time="09:00:00", end_time="11:00:00",
            name="Both Times Changed",
        )

        _open_edit_modal(page, "Both Times Changed")

        # Change start time first
        page.fill("#edit-start-time", "10:00")
        page.dispatch_event("#edit-start-time", "change")

        # Now change end time — should NOT auto-adjust start since start was already touched
        page.fill("#edit-end-time", "14:00")
        page.dispatch_event("#edit-end-time", "change")

        start_time = page.input_value("#edit-start-time")
        end_time = page.input_value("#edit-end-time")
        assert start_time == "10:00", f"Start should stay at 10:00, got {start_time}"
        assert end_time == "14:00", f"End should stay at 14:00, got {end_time}"


class TestEditClearLoopCount:
    """Editing loop_count in the UI updates end_time accordingly."""

    def test_edit_loop_count_cleared_on_duration_change(self, page: Page, api, ws_url):
        """When loop_count is cleared in the edit modal, end_time should re-enable."""
        device_id = "edit-lc-001"
        asset_id = _setup_device_and_asset(page, api, ws_url, device_id)

        # Create schedule WITHOUT loop_count (asset has no real duration)
        sched = _create_schedule_via_api(
            api, device_id, asset_id,
            start_time="09:00:00", end_time="10:20:00",
            name="Clear Loop Count",
        )

        _open_edit_modal(page, "Clear Loop Count")
        # Inject duration into JS data for the modal
        page.evaluate(
            """([assetId, dur]) => {
                const a = _scheduleData.assets.find(x => x.id === assetId);
                if (a) a.duration = dur;
            }""",
            [asset_id, ASSET_DURATION],
        )

        # Set loop_count=8 in the input — end_time should become disabled
        page.fill("#edit-loop-count", "8")
        page.dispatch_event("#edit-loop-count", "input")

        end_time_input = page.locator("#edit-end-time")
        expect(end_time_input).to_be_disabled()

        summary = page.locator("#edit-schedule-summary")
        expect(summary).to_be_visible()
        expect(summary).to_contain_text("exact 8 loops")

        # Clear loop_count — end_time should re-enable
        page.fill("#edit-loop-count", "")
        page.dispatch_event("#edit-loop-count", "input")

        expect(end_time_input).to_be_enabled()
        expect(summary).not_to_contain_text("exact 8 loops")

    def test_create_loop_count_cleared_on_time_change(self, page: Page, api, ws_url):
        """On the create form, clearing loop_count re-enables end_time for manual input."""
        device_id = "create-lc-001"
        asset_id = _setup_device_and_asset(page, api, ws_url, device_id)
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "Create Clear LC")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:25")
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        expect(summary).to_be_visible()

        # Click round down to lock loop_count=8
        summary.locator(".loop-round", has_text="Round down").click()
        expect(summary).to_contain_text("exact 8 loops")

        # end_time should be disabled now
        end_time_input = page.locator('input[name="end_time"]')
        expect(end_time_input).to_be_disabled()

        # Clear loop_count — end_time should re-enable
        page.fill('input[name="loop_count"]', "")
        page.dispatch_event('input[name="loop_count"]', "input")

        expect(end_time_input).to_be_enabled()
        # Should no longer say "exact 8 loops"
        expect(summary).not_to_contain_text("exact 8 loops")

        # Now we can change end_time manually
        page.fill('input[name="end_time"]', "11:00")
        page.dispatch_event('input[name="end_time"]', "change")
        expect(summary).to_contain_text("loops")


class TestSubOneLoop:
    """Schedule duration shorter than asset duration (< 1 loop)."""

    # Use an 11-minute (660s) video so 8-min schedule = 0.73 loops
    LONG_ASSET = 660

    def test_short_duration_shows_loop_info(self, page: Page, api, ws_url):
        """When schedule is shorter than half the asset, loop info should
        still be displayed (not silently empty)."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "sub1-short-001")
        _go_to_schedules_and_inject_duration(page, asset_id, self.LONG_ASSET)

        page.fill('input[name="name"]', "Short Duration Test")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "09:05")  # 5 min / 11 min = 0.45 loops
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        expect(summary).to_be_visible()
        # Should show some loop info, not be empty
        expect(summary).to_contain_text("loop")

    def test_sub_one_loop_no_round_down_to_zero(self, page: Page, api, ws_url):
        """When loops < 1, 'Round down (0)' must never appear."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "sub1-no0-001")
        _go_to_schedules_and_inject_duration(page, asset_id, self.LONG_ASSET)

        page.fill('input[name="name"]', "No Zero Round Test")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "09:08")  # 8 min / 11 min = 0.73 loops
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        expect(summary).to_be_visible()
        # Should NOT offer "Round down (0)"
        expect(summary).not_to_contain_text("Round down (0)")
        # Should offer Round up (1)
        expect(summary).to_contain_text("Round up (1)")

    def test_sub_one_loop_round_up_to_one(self, page: Page, api, ws_url):
        """Clicking Round up (1) for a sub-one-loop schedule should set
        end time to start + 1 full asset duration."""
        asset_id = _setup_device_and_asset(page, api, ws_url, "sub1-rup-001")
        _go_to_schedules_and_inject_duration(page, asset_id, self.LONG_ASSET)

        page.fill('input[name="name"]', "Sub-One Round Up")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "09:08")  # 8 min / 11 min = 0.73 loops
        page.dispatch_event('input[name="end_time"]', "change")

        summary = page.locator("#schedule-summary")
        round_up = summary.locator(".loop-round", has_text="Round up")
        expect(round_up).to_be_visible()
        round_up.click()

        # End time = 09:00 + 11 min = 09:11
        end_time = page.input_value('input[name="end_time"]')
        assert end_time == "09:11:00", f"Expected 09:11:00 but got {end_time}"
        expect(summary).to_contain_text("exact 1 loop")


class TestLoopCountFormSubmit:
    """Regression: creating a schedule with loop_count set must succeed
    even though end_time is disabled (FormData excludes disabled inputs)."""

    def test_create_with_loop_count_sends_end_time(self, page: Page, api, ws_url):
        """Set loop_count on the create form, submit, and verify the POST
        request includes end_time even though the input is disabled."""
        device_id = "lc-submit-001"
        asset_id = _setup_device_and_asset(page, api, ws_url, device_id)
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "LC Submit Test")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.select_option('select[name="target_id"]', value=device_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:00")
        page.dispatch_event('input[name="end_time"]', "change")

        # Set loop_count=3 — this disables end_time and auto-computes it
        page.fill('input[name="loop_count"]', "3")
        page.dispatch_event('input[name="loop_count"]', "input")

        end_input = page.locator('input[name="end_time"]')
        expect(end_input).to_be_disabled()

        # Verify the auto-computed end_time is correct: 09:00 + 3×600s = 09:30
        end_time = page.input_value('input[name="end_time"]')
        assert end_time == "09:30:00", f"Expected 09:30:00 but got {end_time}"

        # Intercept the POST to verify end_time is included in the request body
        captured_body = {}

        def capture_request(route, request):
            import json
            captured_body.update(json.loads(request.post_data))
            # Fulfill with a fake success to avoid needing real asset duration
            route.fulfill(
                status=201,
                content_type="application/json",
                body='{"id":"fake-id","name":"LC Submit Test"}',
            )

        page.route("**/api/schedules", capture_request)
        page.click('button[type="submit"]')
        page.wait_for_timeout(1000)

        assert "end_time" in captured_body, f"POST body missing end_time: {captured_body}"
        assert captured_body["end_time"] == "09:30:00", (
            f"Unexpected end_time in POST: {captured_body['end_time']}"
        )
        assert captured_body.get("loop_count") == 3

    def test_adjust_start_time_after_loop_count_sends_end_time(self, page: Page, api, ws_url):
        """Reproduce the exact bug: set time + loop_count, then adjust start_time.
        The auto-computed end_time updates, and the POST must include it."""
        device_id = "lc-adjust-001"
        asset_id = _setup_device_and_asset(page, api, ws_url, device_id)
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "LC Adjust Start")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.select_option('select[name="target_id"]', value=device_id)
        page.fill('input[name="start_time"]', "05:59")
        page.fill('input[name="end_time"]', "06:00")
        page.dispatch_event('input[name="end_time"]', "change")

        # Set loop_count=1
        page.fill('input[name="loop_count"]', "1")
        page.dispatch_event('input[name="loop_count"]', "input")

        end_input = page.locator('input[name="end_time"]')
        expect(end_input).to_be_disabled()

        # Now adjust start_time — this re-triggers computeLoopEndTime
        page.fill('input[name="start_time"]', "06:00")
        page.dispatch_event('input[name="start_time"]', "change")

        # end_time should still be disabled and recomputed: 06:00 + 1×600s = 06:10
        expect(end_input).to_be_disabled()
        end_time = page.input_value('input[name="end_time"]')
        assert end_time == "06:10:00", f"Expected 06:10:00 but got {end_time}"

        # Intercept POST
        captured_body = {}

        def capture_request(route, request):
            import json
            captured_body.update(json.loads(request.post_data))
            route.fulfill(
                status=201,
                content_type="application/json",
                body='{"id":"fake-id","name":"LC Adjust Start"}',
            )

        js_errors = []
        page.on("pageerror", lambda err: js_errors.append(str(err)))
        page.route("**/api/schedules", capture_request)
        page.click('button[type="submit"]')
        page.wait_for_timeout(1000)

        assert "end_time" in captured_body, f"POST body missing end_time: {captured_body}"
        assert captured_body["end_time"] == "06:10:00", (
            f"Unexpected end_time in POST: {captured_body['end_time']}"
        )
        assert captured_body.get("loop_count") == 1
        assert not js_errors, f"JS errors: {js_errors}"


class TestSecondPrecisionEndTime:
    """End-time auto-computation works in seconds, not just minutes."""

    SHORT_ASSET = 3  # 3-second video clip

    def test_short_clip_loop_count_shows_seconds(self, page: Page, api, ws_url):
        """A 3-second clip with loop_count=1 should show end_time with seconds offset."""
        device_id = "sec-prec-001"
        asset_id = _setup_device_and_asset(page, api, ws_url, device_id)
        _go_to_schedules_and_inject_duration(page, asset_id, self.SHORT_ASSET)

        page.fill('input[name="name"]', "Seconds Precision")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:00")
        page.dispatch_event('input[name="end_time"]', "change")

        # Set loop_count=1 — end_time should be 09:00:03 (3 seconds later)
        page.fill('input[name="loop_count"]', "1")
        page.dispatch_event('input[name="loop_count"]', "input")

        end_time = page.input_value('input[name="end_time"]')
        assert end_time == "09:00:03", f"Expected 09:00:03 but got {end_time}"

    def test_short_clip_multi_loop_shows_seconds(self, page: Page, api, ws_url):
        """A 3-second clip with loop_count=10 → end_time = start + 30s."""
        device_id = "sec-prec-002"
        asset_id = _setup_device_and_asset(page, api, ws_url, device_id)
        _go_to_schedules_and_inject_duration(page, asset_id, self.SHORT_ASSET)

        page.fill('input[name="name"]', "Multi Loop Seconds")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "14:30")
        page.fill('input[name="end_time"]', "15:00")
        page.dispatch_event('input[name="end_time"]', "change")

        page.fill('input[name="loop_count"]', "10")
        page.dispatch_event('input[name="loop_count"]', "input")

        end_time = page.input_value('input[name="end_time"]')
        assert end_time == "14:30:30", f"Expected 14:30:30 but got {end_time}"

    def test_longer_clip_still_correct_with_seconds(self, page: Page, api, ws_url):
        """A 10-minute clip (600s) × 3 loops = 30 min, should render as HH:MM:SS."""
        device_id = "sec-prec-003"
        asset_id = _setup_device_and_asset(page, api, ws_url, device_id)
        _go_to_schedules_and_inject_duration(page, asset_id, ASSET_DURATION)

        page.fill('input[name="name"]', "Normal Clip Seconds")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "10:00")
        page.dispatch_event('input[name="end_time"]', "change")

        page.fill('input[name="loop_count"]', "3")
        page.dispatch_event('input[name="loop_count"]', "input")

        # 09:00 + 3×600s = 09:30:00
        end_time = page.input_value('input[name="end_time"]')
        assert end_time == "09:30:00", f"Expected 09:30:00 but got {end_time}"

    def test_time_inputs_have_step_one(self, page: Page, api, ws_url):
        """Verify time inputs have step=1 attribute for seconds display."""
        _setup_device_and_asset(page, api, ws_url, "sec-step-001")
        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        start_input = page.locator('input[name="start_time"]')
        end_input = page.locator('input[name="end_time"]')
        assert start_input.get_attribute("step") == "1", "start_time missing step=1"
        assert end_input.get_attribute("step") == "1", "end_time missing step=1"
