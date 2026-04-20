"""Playwright tests for the Expired Schedules panel on the Schedules page."""

from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async, click_row_action
from tests_e2e.fake_device import FakeDevice


def _ensure_device_and_asset(api, ws_url, device_id):
    """Register + adopt a device and ensure at least one asset exists."""
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
        api.create_asset("e2e-expired-test.mp4")
        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("Could not create test asset")

    return assets.json()[0], group_id


class TestExpiredSchedulesPanel:
    """Expired schedules panel visibility and behaviour."""

    def test_expired_schedule_in_expired_panel(self, page: Page, api, ws_url):
        """A schedule with past end_date appears under 'Expired Schedules'."""
        asset, group_id = _ensure_device_and_asset(api, ws_url, "exp-e2e-001")

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%dT00:00:00Z")
        resp = api.post("/api/schedules", json={
            "name": "E2E Past Event",
            "group_id": group_id,
            "asset_id": asset["id"],
            "start_time": "09:00",
            "end_time": "17:00",
            "start_date": week_ago,
            "end_date": yesterday,
        })
        assert resp.status_code == 201

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        expired_card = page.locator(".card", has_text="Expired Schedules")
        expect(expired_card).to_be_visible()
        expect(expired_card.locator("td", has_text="E2E Past Event")).to_be_visible()

        # Should NOT appear in the Active Schedules card
        active_card = page.locator(".card", has_text="Active Schedules")
        expect(active_card.locator("td", has_text="E2E Past Event")).not_to_be_visible()

    def test_no_expired_panel_without_expired_schedules(self, page: Page, api, ws_url):
        """Panel should not appear when all schedules are active."""
        _, group_id = _ensure_device_and_asset(api, ws_url, "exp-e2e-002")

        # Clean up any existing schedules
        for s in api.get("/api/schedules").json():
            api.delete(f"/api/schedules/{s['id']}")

        # Create only an active schedule (no end date)
        asset = api.get("/api/assets").json()[0]
        api.post("/api/schedules", json={
            "name": "E2E Active Only",
            "group_id": group_id,
            "asset_id": asset["id"],
            "start_time": "08:00",
            "end_time": "20:00",
        })

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        expect(page.locator(".card", has_text="Active Schedules")).to_be_visible()
        expect(page.locator(".card", has_text="Expired Schedules")).not_to_be_visible()

    def test_expired_schedule_edit_button_works(self, page: Page, api, ws_url):
        """Clicking Edit on an expired schedule should open the edit modal."""
        asset, group_id = _ensure_device_and_asset(api, ws_url, "exp-e2e-003")

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
        api.post("/api/schedules", json={
            "name": "E2E Editable Expired",
            "group_id": group_id,
            "asset_id": asset["id"],
            "start_time": "10:00",
            "end_time": "11:00",
            "start_date": yesterday,
            "end_date": yesterday,
        })

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        expired_card = page.locator(".card", has_text="Expired Schedules")
        row = expired_card.locator("tr", has_text="E2E Editable Expired")
        click_row_action(row, "Edit")

        # The edit modal should appear
        modal = page.locator(".modal-overlay")
        expect(modal).to_be_visible(timeout=3000)

    def test_expired_schedule_delete(self, page: Page, api, ws_url):
        """Deleting an expired schedule should remove it from the panel."""
        asset, group_id = _ensure_device_and_asset(api, ws_url, "exp-e2e-004")

        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT00:00:00Z")
        resp = api.post("/api/schedules", json={
            "name": "E2E Delete Expired",
            "group_id": group_id,
            "asset_id": asset["id"],
            "start_time": "14:00",
            "end_time": "15:00",
            "start_date": yesterday,
            "end_date": yesterday,
        })
        assert resp.status_code == 201

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        expired_card = page.locator(".card", has_text="Expired Schedules")
        row = expired_card.locator("tr", has_text="E2E Delete Expired")

        click_row_action(row, "Delete")

        # Confirm the custom modal
        confirm_modal = page.locator(".modal-overlay")
        expect(confirm_modal).to_be_visible(timeout=3000)
        confirm_modal.locator("button", has_text="Confirm").click()
        page.wait_for_load_state("networkidle")

        # Schedule should be gone from the API
        schedules = api.get("/api/schedules").json()
        names = [s["name"] for s in schedules]
        assert "E2E Delete Expired" not in names
