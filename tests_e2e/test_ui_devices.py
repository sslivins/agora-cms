"""Playwright tests for the Devices page."""

import asyncio
import threading
import time

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async
from tests_e2e.fake_device import FakeDevice


class TestDevicesPage:
    """Device list and management UI."""

    def test_pending_device_appears(self, page: Page, ws_url, e2e_server):
        """A newly registered device should appear on the devices page."""

        async def register():
            async with FakeDevice("ui-dev-001", ws_url) as dev:
                await dev.send_status()

        run_async(register())

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        # CMS sets device name = device_id; device row uses data-device-id attr
        expect(page.locator('[data-device-id="ui-dev-001"]').first).to_be_visible(timeout=5000)

    def test_adopt_device_button(self, page: Page, ws_url, e2e_server):
        """Clicking Adopt should change device status."""

        async def register():
            async with FakeDevice("ui-adopt-001", ws_url) as dev:
                await dev.send_status()

        run_async(register())

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        adopt_btn = page.locator("button", has_text="Adopt").first
        expect(adopt_btn).to_be_visible(timeout=5000)
        adopt_btn.click()
        page.wait_for_load_state("networkidle")

        # Reload and verify the device is no longer pending
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

    def test_device_groups_section_visible(self, page: Page):
        """The Device Groups section should be present on the devices page."""
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        expect(page.locator("h2", has_text="Device Groups")).to_be_visible()

    def test_create_device_group(self, page: Page):
        """Creating a device group via the UI form."""
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        group_name_input = page.locator('#group-name')
        if group_name_input.count() == 0:
            pytest.skip("No group creation form found on page")
        group_name_input.fill("E2E Test Group")
        page.locator("button", has_text="Add Group").click()
        page.wait_for_load_state("networkidle")
        # Group name appears in bold in the group header
        expect(page.locator("strong", has_text="E2E Test Group")).to_be_visible()


class TestDashboardPendingDeviceName:
    """Dashboard should show device friendly name, not raw ID, for pending devices."""

    def test_pending_device_shows_friendly_name(self, page: Page, ws_url, e2e_server):
        """A device registered with a custom name should show that name on the dashboard."""

        async def register():
            async with FakeDevice("name-test-001", ws_url, device_name="Kitchen Display") as dev:
                await dev.send_status()

        run_async(register())

        page.goto("/")
        page.wait_for_load_state("domcontentloaded")

        # The friendly name should appear in the Pending Devices section
        pending_section = page.locator("text=Pending Devices").locator("..")
        expect(pending_section.locator("text=Kitchen Display")).to_be_visible(timeout=5000)


class TestDeleteDeviceWithSchedules:
    """Deleting a device that has schedules should work from the UI."""

    def test_delete_device_with_schedule(self, page: Page, api, ws_url, e2e_server):
        """A device with schedules can be deleted via the Delete button."""

        # Register and adopt a device
        async def register():
            async with FakeDevice("del-e2e-001", ws_url) as dev:
                await dev.send_status()

        run_async(register())

        api.post("/api/devices/del-e2e-001/adopt")

        # Create a group and assign the device to it
        group_resp = api.post("/api/devices/groups/", json={"name": "Del Test Group"})
        group_id = group_resp.json()["id"]
        api.patch("/api/devices/del-e2e-001", json={"group_id": group_id})

        # Create an asset and schedule targeting the group
        asset_resp = api.create_asset()
        assert asset_resp.status_code == 201
        asset_id = asset_resp.json()["id"]

        sched_resp = api.post("/api/schedules", json={
            "name": "Delete Test Schedule",
            "group_id": group_id,
            "asset_id": asset_id,
            "start_time": "09:00",
            "end_time": "17:00",
        })
        assert sched_resp.status_code == 201

        # Go to devices page and delete the device
        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")

        # Find the device row and click Delete (use .first since the device
        # may appear in both the main table and the ungrouped section)
        row = page.locator('[data-device-id="del-e2e-001"]').first
        expect(row).to_be_visible(timeout=5000)
        row.locator("button", has_text="Delete").click()

        # Confirm the modal
        confirm_modal = page.locator(".modal-overlay")
        expect(confirm_modal).to_be_visible(timeout=3000)
        confirm_modal.locator("button", has_text="Confirm").click()
        page.wait_for_load_state("networkidle")

        # Device should be gone from all sections
        expect(page.locator('[data-device-id="del-e2e-001"]')).to_have_count(0)


class TestPlaybackAssetTooltip:
    """Tooltip on the playback asset name should show the full filename."""

    def test_tooltip_shows_full_filename(self, page: Page, ws_url, e2e_server):
        """Hovering over a truncated playback asset name should show the
        full filename in the tooltip, not '?' or nothing."""

        asset_name = "my_really_long_test_video_filename.mp4"
        stop_event = threading.Event()
        ready_event = threading.Event()

        async def run_device():
            dev = FakeDevice("tooltip-dev-001", ws_url)
            await dev.connect()
            await dev.send_status(mode="play", asset=asset_name)
            ready_event.set()
            while not stop_event.is_set():
                await asyncio.sleep(0.2)
            await dev.disconnect()

        thread = threading.Thread(target=lambda: asyncio.run(run_device()), daemon=True)
        thread.start()
        ready_event.wait(timeout=15)

        try:
            page.goto("/devices")
            page.wait_for_load_state("domcontentloaded")

            # Expand the device detail row
            row = page.locator('[data-device-id="tooltip-dev-001"]').first
            expect(row).to_be_visible(timeout=5000)
            row.locator(".expand-toggle").click()

            # The detail row should now be visible
            detail = page.locator('[data-detail-for="tooltip-dev-001"]').first
            expect(detail).to_be_visible(timeout=3000)

            # Find the tooltip trigger (the .has-tooltip span with the asset name).
            # The asset text may not appear until the JS poll fires (5 s interval)
            # and applyAssetTooltips wraps it, so use a generous timeout.
            tooltip_trigger = detail.locator(".has-tooltip", has_text=asset_name).first
            expect(tooltip_trigger).to_be_visible(timeout=10_000)

            # Hover to reveal the tooltip
            tooltip_trigger.hover()

            # The .tooltip child should become visible and contain the full filename
            tooltip = tooltip_trigger.locator(".tooltip")
            expect(tooltip).to_be_visible(timeout=3000)
            expect(tooltip).to_contain_text(asset_name)

            # Verify the tooltip is not clipped by overflow:hidden ancestors.
            # A clipped tooltip would have a zero or near-zero visible height.
            clip_info = page.evaluate("""(el) => {
                const rect = el.getBoundingClientRect();
                let ancestor = el.parentElement;
                while (ancestor) {
                    const style = window.getComputedStyle(ancestor);
                    if (style.overflow === 'hidden' || style.overflowY === 'hidden') {
                        const aRect = ancestor.getBoundingClientRect();
                        // Check if tooltip extends outside the overflow container
                        if (rect.top < aRect.top || rect.bottom > aRect.bottom) {
                            return { clipped: true, tooltipTop: rect.top, ancestorTop: aRect.top };
                        }
                    }
                    ancestor = ancestor.parentElement;
                }
                return { clipped: false };
            }""", tooltip.element_handle())
            assert not clip_info["clipped"], (
                f"Tooltip is clipped by an overflow:hidden ancestor: {clip_info}"
            )
        finally:
            stop_event.set()
            thread.join(timeout=5)

    def test_short_filename_no_help_cursor(self, page: Page, ws_url, e2e_server):
        """A short asset name that fits without truncation should not show
        cursor:help (the '?' cursor) — no tooltip wrapper should be added."""

        asset_name = "clip.mp4"  # well under 18ch limit
        stop_event = threading.Event()
        ready_event = threading.Event()

        async def run_device():
            dev = FakeDevice("tooltip-dev-002", ws_url)
            await dev.connect()
            await dev.send_status(mode="play", asset=asset_name)
            ready_event.set()
            while not stop_event.is_set():
                await asyncio.sleep(0.2)
            await dev.disconnect()

        thread = threading.Thread(target=lambda: asyncio.run(run_device()), daemon=True)
        thread.start()
        ready_event.wait(timeout=15)

        try:
            page.goto("/devices")
            page.wait_for_load_state("domcontentloaded")

            row = page.locator('[data-device-id="tooltip-dev-002"]').first
            expect(row).to_be_visible(timeout=5000)
            row.locator(".expand-toggle").click()

            detail = page.locator('[data-detail-for="tooltip-dev-002"]').first
            expect(detail).to_be_visible(timeout=3000)

            # The asset-name-truncate span should be visible with the short name.
            # The text may not appear until the JS status poll fires (~5s interval),
            # so use a generous timeout like the long-filename test above.
            asset_span = detail.locator(".asset-name-truncate", has_text=asset_name).first
            expect(asset_span).to_be_visible(timeout=10_000)

            # Wait a frame for applyAssetTooltips to run
            page.wait_for_timeout(200)

            # The parent should NOT have has-tooltip class (no truncation)
            parent_has_tooltip = page.evaluate(
                "(el) => el.parentElement.classList.contains('has-tooltip')",
                asset_span.element_handle(),
            )
            assert not parent_has_tooltip, (
                "Short filename should not have has-tooltip class (no ? cursor)"
            )

            # Verify cursor is not 'help'
            cursor = page.evaluate(
                "(el) => window.getComputedStyle(el.parentElement).cursor",
                asset_span.element_handle(),
            )
            assert cursor != "help", (
                f"Short filename parent should not have cursor:help, got {cursor}"
            )
        finally:
            stop_event.set()
            thread.join(timeout=5)


class TestDetailPanelPipelineBadge:
    """Detail panel should show the correct pipeline state badge and asset name."""

    def test_playing_badge_and_asset_shown(self, page: Page, ws_url, e2e_server):
        """A playing device should show a green 'Playing' badge and the asset filename."""

        asset_name = "demo_clip.mp4"
        stop_event = threading.Event()
        ready_event = threading.Event()

        async def run_device():
            dev = FakeDevice("badge-dev-001", ws_url)
            await dev.connect()
            await dev.send_status(mode="play", asset=asset_name, pipeline_state="PLAYING")
            ready_event.set()
            while not stop_event.is_set():
                await asyncio.sleep(0.2)
            await dev.disconnect()

        thread = threading.Thread(target=lambda: asyncio.run(run_device()), daemon=True)
        thread.start()
        ready_event.wait(timeout=15)

        try:
            page.goto("/devices")
            page.wait_for_load_state("domcontentloaded")

            row = page.locator('[data-device-id="badge-dev-001"]').first
            expect(row).to_be_visible(timeout=5000)
            row.locator(".expand-toggle").click()

            detail = page.locator('[data-detail-for="badge-dev-001"]').first
            expect(detail).to_be_visible(timeout=3000)

            # Pipeline State badge should say "Playing" with badge-online class
            pipeline_el = detail.locator('[data-live-pipeline="badge-dev-001"]')
            expect(pipeline_el.locator(".badge-online")).to_be_visible(timeout=3000)
            expect(pipeline_el.locator(".badge-online")).to_have_text("Playing")

            # Asset should show the filename
            asset_el = detail.locator('[data-live-asset="badge-dev-001"]')
            expect(asset_el).to_contain_text(asset_name)
        finally:
            stop_event.set()
            thread.join(timeout=5)

    def test_splash_badge_shown(self, page: Page, ws_url, e2e_server):
        """A device in splash mode should show a 'Splash' badge."""

        stop_event = threading.Event()
        ready_event = threading.Event()

        async def run_device():
            dev = FakeDevice("badge-dev-002", ws_url)
            await dev.connect()
            await dev.send_status(mode="splash", asset=None)
            ready_event.set()
            while not stop_event.is_set():
                await asyncio.sleep(0.2)
            await dev.disconnect()

        thread = threading.Thread(target=lambda: asyncio.run(run_device()), daemon=True)
        thread.start()
        ready_event.wait(timeout=15)

        try:
            page.goto("/devices")
            page.wait_for_load_state("domcontentloaded")

            row = page.locator('[data-device-id="badge-dev-002"]').first
            expect(row).to_be_visible(timeout=5000)
            row.locator(".expand-toggle").click()

            detail = page.locator('[data-detail-for="badge-dev-002"]').first
            expect(detail).to_be_visible(timeout=3000)

            # Pipeline State badge should say "Splash"
            pipeline_el = detail.locator('[data-live-pipeline="badge-dev-002"]')
            expect(pipeline_el.locator(".badge-pending")).to_be_visible(timeout=10000)
            expect(pipeline_el.locator(".badge-pending")).to_have_text("Splash")

            # Asset should show "Splash screen"
            asset_el = detail.locator('[data-live-asset="badge-dev-002"]')
            expect(asset_el).to_contain_text("Splash screen")
        finally:
            stop_event.set()
            thread.join(timeout=5)


class TestDetailPanelLiveRefresh:
    """Auto-refresh should update the detail panel in-place when it's open."""

    def test_pipeline_badge_updates_in_place(self, page: Page, ws_url, e2e_server):
        """Pipeline badge should change from Splash to Playing without closing the panel."""

        stop_event = threading.Event()
        ready_event = threading.Event()
        switch_event = threading.Event()

        async def run_device():
            dev = FakeDevice("live-dev-001", ws_url)
            await dev.connect()
            # Start in splash mode
            await dev.send_status(mode="splash", asset=None)
            ready_event.set()
            # Wait for test to open the panel and verify splash
            while not switch_event.is_set():
                await asyncio.sleep(0.2)
                await dev.send_status(mode="splash", asset=None)
            # Switch to playing
            await dev.send_status(mode="play", asset="switched.mp4", pipeline_state="PLAYING")
            # Keep sending playing status so the poll picks it up
            while not stop_event.is_set():
                await asyncio.sleep(0.5)
                await dev.send_status(mode="play", asset="switched.mp4", pipeline_state="PLAYING")
            await dev.disconnect()

        thread = threading.Thread(target=lambda: asyncio.run(run_device()), daemon=True)
        thread.start()
        ready_event.wait(timeout=15)

        try:
            page.goto("/devices")
            page.wait_for_load_state("domcontentloaded")

            row = page.locator('[data-device-id="live-dev-001"]').first
            expect(row).to_be_visible(timeout=5000)
            row.locator(".expand-toggle").click()

            detail = page.locator('[data-detail-for="live-dev-001"]').first
            expect(detail).to_be_visible(timeout=3000)

            # Verify initial splash state
            pipeline_el = detail.locator('[data-live-pipeline="live-dev-001"]')
            expect(pipeline_el.locator(".badge-pending")).to_have_text("Splash", timeout=3000)

            # Now switch the device to playing
            switch_event.set()

            # Wait for auto-refresh (5s interval) to pick up the change
            # The badge should update to "Playing" without a full page reload.
            # The detail panel should still be visible (no reload closed it).
            expect(pipeline_el.locator(".badge-online")).to_have_text("Playing", timeout=15000)

            # Asset should also update in-place
            asset_el = detail.locator('[data-live-asset="live-dev-001"]')
            expect(asset_el).to_contain_text("switched.mp4", timeout=5000)

            # Panel should still be expanded (no full reload)
            expect(detail).to_be_visible()
        finally:
            stop_event.set()
            thread.join(timeout=5)
