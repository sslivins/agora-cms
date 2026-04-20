"""Regression tests for the schedules-page "no page reload" refactor (#87).

Every covered action used to call location.reload() on success. Each test
seeds a window-scoped sentinel, performs the action, and asserts the DOM
mutated in place (sentinel survived, URL unchanged). One extra test
covers the 5s poller — a schedule created out-of-band by another CMS
replica must appear in the active table without a user-initiated reload.
"""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async, click_row_action
from tests_e2e.fake_device import FakeDevice


SENTINEL_SETUP = """
    () => {
        window.__noReloadSentinel = Math.random().toString(36).slice(2);
        return window.__noReloadSentinel;
    }
"""

SENTINEL_CHECK = "() => window.__noReloadSentinel"


def _install_sentinel(page: Page) -> str:
    return page.evaluate(SENTINEL_SETUP)


def _assert_no_reload(page: Page, original_url: str, sentinel: str) -> None:
    current = page.evaluate(SENTINEL_CHECK)
    assert current == sentinel, (
        f"Page reloaded: sentinel lost (was {sentinel!r}, now {current!r})"
    )
    assert page.url == original_url, (
        f"Page navigated: {original_url} -> {page.url}"
    )


def _ensure_device_and_asset(api, ws_url, device_id):
    async def register():
        async with FakeDevice(device_id, ws_url) as dev:
            await dev.send_status()

    run_async(register())
    api.post(f"/api/devices/{device_id}/adopt")

    group_resp = api.post(
        "/api/devices/groups/", json={"name": f"Group-{device_id}"}
    )
    group_id = group_resp.json()["id"]
    api.patch(f"/api/devices/{device_id}", json={"group_id": group_id})

    assets = api.get("/api/assets")
    if not assets.json():
        api.create_asset("e2e-shared-test.mp4")
        assets = api.get("/api/assets")
        if not assets.json():
            pytest.skip("Could not create test asset (ffprobe not available)")

    asset = assets.json()[0]
    return asset["id"], group_id


def _create_schedule(api, *, name, asset_id, group_id, priority=0):
    resp = api.post(
        "/api/schedules",
        json={
            "name": name,
            "asset_id": asset_id,
            "group_id": group_id,
            "start_time": "08:00:00",
            "end_time": "17:00:00",
            "priority": priority,
            "enabled": True,
        },
    )
    assert resp.status_code in (200, 201), resp.text
    return resp.json()["id"]


class TestSchedulesNoReload:
    """Each covered action must update the DOM inline, not reload."""

    def test_delete_schedule_no_reload(self, page: Page, api, ws_url, e2e_server):
        asset_id, group_id = _ensure_device_and_asset(api, ws_url, "nrl-sch-del")
        keep_id = _create_schedule(api, name="Keeper", asset_id=asset_id, group_id=group_id)
        gone_id = _create_schedule(api, name="ToDelete", asset_id=asset_id, group_id=group_id)

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")
        expect(page.locator(f'tr[data-schedule-id="{gone_id}"]')).to_be_visible(
            timeout=5000
        )

        original_url = page.url
        sentinel = _install_sentinel(page)

        row = page.locator(f'tr[data-schedule-id="{gone_id}"]')
        click_row_action(row, "Delete")

        confirm_modal = page.locator(".modal-overlay")
        expect(confirm_modal).to_be_visible(timeout=3000)
        confirm_modal.locator("button", has_text="Confirm").click()

        expect(page.locator(f'tr[data-schedule-id="{gone_id}"]')).to_have_count(
            0, timeout=5000
        )
        expect(page.locator(f'tr[data-schedule-id="{keep_id}"]')).to_be_visible()

        _assert_no_reload(page, original_url, sentinel)

    def test_toggle_schedule_no_reload(self, page: Page, api, ws_url, e2e_server):
        asset_id, group_id = _ensure_device_and_asset(api, ws_url, "nrl-sch-tog")
        sched_id = _create_schedule(
            api, name="ToggleMe", asset_id=asset_id, group_id=group_id
        )

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator(f'tr[data-schedule-id="{sched_id}"]')
        expect(row.locator("button", has_text="On")).to_be_visible(timeout=5000)

        original_url = page.url
        sentinel = _install_sentinel(page)

        row.locator("button", has_text="On").click()

        # Row is swapped in place; button flips to "Off".
        expect(
            page.locator(f'tr[data-schedule-id="{sched_id}"] button', has_text="Off")
        ).to_be_visible(timeout=5000)

        _assert_no_reload(page, original_url, sentinel)

    def test_create_schedule_no_reload(self, page: Page, api, ws_url, e2e_server):
        asset_id, group_id = _ensure_device_and_asset(api, ws_url, "nrl-sch-new")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        original_url = page.url
        sentinel = _install_sentinel(page)

        page.fill('input[name="name"]', "Inline Created")
        page.select_option('select[name="asset_id"]', value=asset_id)
        page.select_option('select[name="group_id"]', value=group_id)
        page.fill('input[name="start_time"]', "09:00")
        page.fill('input[name="end_time"]', "17:00")
        page.click('button[type="submit"]')

        active_card = page.locator(".card", has_text="Active Schedules")
        expect(
            active_card.locator("td", has_text="Inline Created")
        ).to_be_visible(timeout=5000)

        _assert_no_reload(page, original_url, sentinel)

    def test_edit_schedule_no_reload(self, page: Page, api, ws_url, e2e_server):
        asset_id, group_id = _ensure_device_and_asset(api, ws_url, "nrl-sch-edit")
        sched_id = _create_schedule(
            api, name="OriginalName", asset_id=asset_id, group_id=group_id
        )

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator(f'tr[data-schedule-id="{sched_id}"]')
        expect(row).to_be_visible(timeout=5000)

        original_url = page.url
        sentinel = _install_sentinel(page)

        click_row_action(row, "Edit")
        modal = page.locator(".modal-overlay")
        expect(modal).to_be_visible(timeout=3000)
        modal.locator('#edit-name').fill("RenamedInline")
        modal.locator("button", has_text="Save").click()

        # Modal closes, row reflects the new name without reload.
        expect(modal).to_have_count(0, timeout=5000)
        expect(
            page.locator(f'tr[data-schedule-id="{sched_id}"] td', has_text="RenamedInline")
        ).to_be_visible(timeout=5000)

        _assert_no_reload(page, original_url, sentinel)

    def test_poller_picks_up_external_create(self, page: Page, api, ws_url, e2e_server):
        """Cross-replica visibility: a schedule created via the API (simulating
        another CMS replica) must appear within ~10s without the user
        touching anything."""
        asset_id, group_id = _ensure_device_and_asset(api, ws_url, "nrl-sch-ext")

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")

        # Create the schedule out-of-band. The poller (5s cycle) should
        # notice the new ID and reload the page to pick it up.
        new_id = _create_schedule(
            api, name="External Create", asset_id=asset_id, group_id=group_id
        )

        # Wait up to ~12s for the poller to reflect the new schedule.
        expect(page.locator(f'tr[data-schedule-id="{new_id}"]')).to_be_visible(
            timeout=12000
        )
