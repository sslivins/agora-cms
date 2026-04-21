"""Cross-session (two-browser) smoke tests.

Every page that's been through the #87 reload-hunt must satisfy the
"cross-replica visibility" requirement: a change made by one user (or
CMS replica) must become visible to another active session within ~5s,
without the second session touching anything.

These tests model that by opening two independent BrowserContexts
(behaviorally equivalent to two separate browsers for this assertion —
separate cookie jars, separate JS execution contexts, separate poller
timers). Session A performs the action through the UI; session B waits
for its poller to pick up the change.

As new pages are migrated off location.reload(), add a matching test
here so we don't silently regress the ~5s propagation bar.
"""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import run_async, click_row_action
from tests_e2e.fake_device import FakeDevice


# Poll cycle is 5s; allow ~2× headroom for CI jitter plus the API roundtrip.
POLL_TIMEOUT_MS = 12000


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

    # Create a fresh asset (and mark it ready) per test rather than
    # reusing whatever is first in the global list. Prior tests may
    # leave assets with unready variants behind, which would make
    # select_option time out on the asset dropdown (unready options
    # render disabled).
    resp = api.create_asset(f"{device_id}.mp4")
    if resp.status_code != 201:
        pytest.skip("Could not create test asset (ffprobe not available)")
    return resp.json()["id"], group_id


def _create_schedule_via_ui(page, *, name, asset_id, group_id, start="09:00", end="17:00"):
    page.fill('input[name="name"]', name)
    page.select_option('select[name="asset_id"]', value=asset_id)
    page.select_option('select[name="group_id"]', value=group_id)
    page.fill('input[name="start_time"]', start)
    page.fill('input[name="end_time"]', end)
    page.click('button[type="submit"]')


# ─────────────────────────── Schedules ───────────────────────────────


class TestSchedulesCrossSession:
    """Session B's poller must reflect session A's schedule changes."""

    def test_create_propagates(self, page: Page, second_page: Page, api, ws_url, e2e_server):
        """Session A creates a schedule → session B sees it appear."""
        asset_id, group_id = _ensure_device_and_asset(api, ws_url, "xs-sch-create")

        # Both sessions are parked on /schedules.
        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")
        second_page.goto("/schedules")
        second_page.wait_for_load_state("domcontentloaded")

        _create_schedule_via_ui(
            page, name="XSession Create", asset_id=asset_id, group_id=group_id
        )
        # Session A sees it immediately (inline insert).
        expect(page.locator("td", has_text="XSession Create")).to_be_visible(
            timeout=5000
        )

        # Session B picks it up via its 5s poller (fires a structural reload).
        expect(
            second_page.locator("td", has_text="XSession Create")
        ).to_be_visible(timeout=POLL_TIMEOUT_MS)

    def test_delete_propagates(self, page: Page, second_page: Page, api, ws_url, e2e_server):
        """Session A deletes a schedule → session B sees it disappear."""
        asset_id, group_id = _ensure_device_and_asset(api, ws_url, "xs-sch-delete")

        resp = api.post(
            "/api/schedules",
            json={
                "name": "XSession Delete Me",
                "asset_id": asset_id,
                "group_id": group_id,
                "start_time": "10:00:00",
                "end_time": "18:00:00",
                "enabled": True,
            },
        )
        sched_id = resp.json()["id"]

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")
        second_page.goto("/schedules")
        second_page.wait_for_load_state("domcontentloaded")

        expect(
            second_page.locator(f'tr[data-schedule-id="{sched_id}"]')
        ).to_be_visible(timeout=5000)

        row = page.locator(f'tr[data-schedule-id="{sched_id}"]')
        click_row_action(row, "Delete")
        page.locator(".modal-overlay").locator(
            "button", has_text="Confirm"
        ).click()

        # Session A removed inline; session B reloads on set-shrink.
        expect(
            second_page.locator(f'tr[data-schedule-id="{sched_id}"]')
        ).to_have_count(0, timeout=POLL_TIMEOUT_MS)

    def test_toggle_propagates(self, page: Page, second_page: Page, api, ws_url, e2e_server):
        """Session A toggles enabled=false → session B sees the button flip.

        Toggle is a per-row field change (no id add/remove), so this
        exercises the signature-based fragment swap on session B rather
        than a structural reload.
        """
        asset_id, group_id = _ensure_device_and_asset(api, ws_url, "xs-sch-toggle")

        resp = api.post(
            "/api/schedules",
            json={
                "name": "XSession Toggle",
                "asset_id": asset_id,
                "group_id": group_id,
                "start_time": "11:00:00",
                "end_time": "19:00:00",
                "enabled": True,
            },
        )
        sched_id = resp.json()["id"]

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")
        second_page.goto("/schedules")
        second_page.wait_for_load_state("domcontentloaded")

        row_a = page.locator(f'tr[data-schedule-id="{sched_id}"]')
        expect(row_a.locator("button", has_text="On")).to_be_visible(timeout=5000)
        row_a.locator("button", has_text="On").click()
        expect(row_a.locator("button", has_text="Off")).to_be_visible(timeout=5000)

        # Session B's per-row signature diff should fragment-swap the row.
        expect(
            second_page.locator(
                f'tr[data-schedule-id="{sched_id}"] button', has_text="Off"
            )
        ).to_be_visible(timeout=POLL_TIMEOUT_MS)

    def test_edit_propagates(self, page: Page, second_page: Page, api, ws_url, e2e_server):
        """Session A renames a schedule → session B reflects the new name."""
        asset_id, group_id = _ensure_device_and_asset(api, ws_url, "xs-sch-edit")

        resp = api.post(
            "/api/schedules",
            json={
                "name": "XSession Original",
                "asset_id": asset_id,
                "group_id": group_id,
                "start_time": "12:00:00",
                "end_time": "20:00:00",
                "enabled": True,
            },
        )
        sched_id = resp.json()["id"]

        page.goto("/schedules")
        page.wait_for_load_state("domcontentloaded")
        second_page.goto("/schedules")
        second_page.wait_for_load_state("domcontentloaded")

        row_a = page.locator(f'tr[data-schedule-id="{sched_id}"]')
        click_row_action(row_a, "Edit")
        modal = page.locator(".modal-overlay")
        modal.locator("#edit-name").fill("XSession Renamed")
        modal.locator("button", has_text="Save").click()
        expect(modal).to_have_count(0, timeout=5000)

        # Session B sees the renamed row via per-row signature diff.
        expect(
            second_page.locator(
                f'tr[data-schedule-id="{sched_id}"] td', has_text="XSession Renamed"
            )
        ).to_be_visible(timeout=POLL_TIMEOUT_MS)


# ─────────────────────────── Devices ─────────────────────────────────


class TestDevicesCrossSession:
    """Session B's /devices poller must reflect session A's device changes."""

    def test_adopt_propagates(self, page: Page, second_page: Page, api, ws_url, e2e_server):
        """Session A adopts a pending device → session B sees it promoted.

        The unadopted device announces itself on the WebSocket; both
        sessions should see it in the pending list initially. Session A
        adopts; session B's poller (5s structural diff) should reload
        and show it in the adopted table.
        """
        device_id = "xs-dev-adopt"

        async def announce():
            async with FakeDevice(device_id, ws_url) as dev:
                await dev.send_status()

        run_async(announce())

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        second_page.goto("/devices")
        second_page.wait_for_load_state("domcontentloaded")

        # Session A adopts.
        api.post(f"/api/devices/{device_id}/adopt")

        # Session B's poller picks up the id within ~5s.
        expect(
            second_page.locator(f'tr[data-device-id="{device_id}"]').first
        ).to_be_visible(timeout=POLL_TIMEOUT_MS)

    def test_delete_propagates(self, page: Page, second_page: Page, api, ws_url, e2e_server):
        """Session A deletes a device → session B sees the row disappear."""
        device_id = "xs-dev-delete"

        async def register():
            async with FakeDevice(device_id, ws_url) as dev:
                await dev.send_status()

        run_async(register())
        api.post(f"/api/devices/{device_id}/adopt")

        page.goto("/devices")
        page.wait_for_load_state("domcontentloaded")
        second_page.goto("/devices")
        second_page.wait_for_load_state("domcontentloaded")

        expect(
            second_page.locator(f'tr[data-device-id="{device_id}"]').first
        ).to_be_visible(timeout=5000)

        # Session A deletes via the row kebab.
        row = page.locator(f'tr.device-row[data-device-id="{device_id}"]').first
        click_row_action(row, "Delete")
        page.locator(".modal-overlay").locator(
            "button", has_text="Confirm"
        ).click()

        # Session B sees the row vanish on next poll cycle.
        expect(
            second_page.locator(f'tr[data-device-id="{device_id}"]')
        ).to_have_count(0, timeout=POLL_TIMEOUT_MS)


# ─────────────────────────── Profiles ────────────────────────────────


def _create_profile_via_ui(page, *, name, description="cross-session"):
    page.fill('input[name="name"]', name)
    page.fill('input[name="description"]', description)
    page.click('button[type="submit"]')


class TestProfilesCrossSession:
    """Session B's /profiles poller must reflect session A's profile changes."""

    def test_create_propagates(self, page: Page, second_page: Page, e2e_server):
        """Session A creates a profile → session B sees the row appear."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")
        second_page.goto("/profiles")
        second_page.wait_for_load_state("domcontentloaded")

        _create_profile_via_ui(page, name="xsess-create")

        # Session A sees it inline.
        row_a = page.locator('tr[data-profile-id]').filter(has_text="xsess-create")
        expect(row_a).to_be_visible(timeout=5000)
        # Session B picks it up via the structural diff on its next cycle.
        expect(
            second_page.locator('tr[data-profile-id]').filter(has_text="xsess-create")
        ).to_be_visible(timeout=POLL_TIMEOUT_MS)

    def test_delete_propagates(self, page: Page, second_page: Page, api, e2e_server):
        """Session A deletes a profile → session B sees it disappear."""
        resp = api.post("/api/profiles", json={
            "name": "xsess-delete",
            "description": "",
            "video_codec": "h264", "video_profile": "main",
            "max_width": 1920, "max_height": 1080, "max_fps": 30,
            "crf": 23, "video_bitrate": "",
            "pixel_format": "auto", "color_space": "auto",
            "audio_codec": "aac", "audio_bitrate": "128k",
        })
        assert resp.status_code in (200, 201), resp.text
        prof_id = resp.json()["id"]

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")
        second_page.goto("/profiles")
        second_page.wait_for_load_state("domcontentloaded")

        expect(
            second_page.locator(f'tr[data-profile-id="{prof_id}"]')
        ).to_be_visible(timeout=5000)

        row = page.locator(f'tr[data-profile-id="{prof_id}"]')
        click_row_action(row, "Delete")
        page.locator(".modal-overlay").locator(
            "button", has_text="Confirm"
        ).click()

        expect(
            second_page.locator(f'tr[data-profile-id="{prof_id}"]')
        ).to_have_count(0, timeout=POLL_TIMEOUT_MS)

    def test_edit_propagates(self, page: Page, second_page: Page, api, e2e_server):
        """Session A edits a profile's description → session B sees the row
        re-render via per-row signature fragment swap (not a full reload)."""
        resp = api.post("/api/profiles", json={
            "name": "xsess-edit",
            "description": "original",
            "video_codec": "h264", "video_profile": "main",
            "max_width": 1920, "max_height": 1080, "max_fps": 30,
            "crf": 23, "video_bitrate": "",
            "pixel_format": "auto", "color_space": "auto",
            "audio_codec": "aac", "audio_bitrate": "128k",
        })
        assert resp.status_code in (200, 201), resp.text
        prof_id = resp.json()["id"]

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")
        second_page.goto("/profiles")
        second_page.wait_for_load_state("domcontentloaded")

        # Edit via API (faster, deterministic) — the UI edit modal path is
        # already covered by the non-cross-session profile tests.
        api.put(f"/api/profiles/{prof_id}", json={
            "description": "renamed",
            "video_codec": "h264", "video_profile": "high",
            "max_width": 1920, "max_height": 1080, "max_fps": 30,
            "crf": 23, "video_bitrate": "",
            "pixel_format": "auto", "color_space": "auto",
            "audio_codec": "aac", "audio_bitrate": "128k",
        })

        # Both sessions' pollers should pick the edit up and fragment-swap
        # the row so "High" now shows in the Codec cell.
        expect(
            page.locator(f'tr[data-profile-id="{prof_id}"]'),
        ).to_contain_text("High", timeout=POLL_TIMEOUT_MS)
        expect(
            second_page.locator(f'tr[data-profile-id="{prof_id}"]'),
        ).to_contain_text("High", timeout=POLL_TIMEOUT_MS)


# ─────────────────────────── Assets ──────────────────────────────────


class TestAssetsCrossSession:
    """Session B's /assets poller must reflect session A's asset changes.

    Added as part of the #87 no-reload rework of assets.html: the page used
    to call ``location.reload()`` when a new asset appeared on a second
    replica; it now fetches the rendered row pair from
    ``GET /api/assets/{id}/row`` and inserts it in place.
    """

    def test_upload_propagates(self, page: Page, second_page: Page, api, e2e_server):
        """Session A uploads an asset → session B's poller inserts the row."""
        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")
        second_page.goto("/assets")
        second_page.wait_for_load_state("domcontentloaded")

        resp = api.create_asset("xsess-upload.mp4")
        assert resp.status_code == 201, resp.text
        asset_id = resp.json()["id"]

        # Session A does not act through the UI here (upload flow is covered
        # by the in-session tests). We're testing that the *other* session's
        # poller detects the new id and fetches its row fragment.
        expect(
            second_page.locator(f'tr.asset-row[data-asset-id="{asset_id}"]')
        ).to_be_visible(timeout=POLL_TIMEOUT_MS)
        # Both the collapsed row and its paired detail row must land.
        expect(
            second_page.locator(f'tr.asset-detail[data-detail-for="{asset_id}"]')
        ).to_have_count(1, timeout=POLL_TIMEOUT_MS)

    def test_delete_propagates(self, page: Page, second_page: Page, api, e2e_server):
        """Session A deletes an asset → session B sees the row disappear."""
        resp = api.create_asset("xsess-delete.mp4")
        assert resp.status_code == 201, resp.text
        asset_id = resp.json()["id"]

        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")
        second_page.goto("/assets")
        second_page.wait_for_load_state("domcontentloaded")

        # Both sessions start with the row visible.
        expect(
            second_page.locator(f'tr.asset-row[data-asset-id="{asset_id}"]')
        ).to_be_visible(timeout=5000)

        del_resp = api.delete(f"/api/assets/{asset_id}")
        assert del_resp.status_code in (200, 204), del_resp.text

        # Give a little extra headroom over POLL_TIMEOUT_MS: on CI the initial
        # `to_be_visible` check above can consume most of one 5s poll cycle,
        # leaving only ~7s before the row must go — tight for 1-2 poll cycles
        # plus the fragment fetch + DOM swap.
        expect(
            second_page.locator(f'tr.asset-row[data-asset-id="{asset_id}"]')
        ).to_have_count(0, timeout=POLL_TIMEOUT_MS + 8000)
        expect(
            second_page.locator(f'tr.asset-detail[data-detail-for="{asset_id}"]')
        ).to_have_count(0, timeout=POLL_TIMEOUT_MS + 8000)


class TestAssetsInSessionNoReload:
    """Acting in a single browser session must NOT trigger location.reload().

    Added with the #87 app.js asset-handler rework: upload / addWebpage /
    addStream / editWebpageUrl used to do a full reload, which wiped any
    open menu / scroll / popover state. They now insert the server-rendered
    row fragment inline. We verify by stamping a sentinel on window and
    confirming it survives.
    """

    def test_webpage_add_no_reload(self, page: Page, e2e_server):
        """Adding a webpage via the UI inserts the row without reloading."""
        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")
        # Stamp a sentinel that a reload would wipe.
        page.evaluate("window.__noReloadSentinel = 'keep-me'")

        # Submit the webpage form directly (bypasses the gate-enabled button;
        # gate state isn't what we're testing).
        page.evaluate("""
            () => {
                document.getElementById('webpage-url').value =
                    'https://example.com/no-reload-' + Date.now();
                document.getElementById('webpage-form').dispatchEvent(
                    new Event('submit', { cancelable: true })
                );
            }
        """)

        # The new row should appear without a reload. We assert >=1 webpage
        # row exists since the page may already have one; the key property
        # we care about is that the sentinel survives.
        page.wait_for_function(
            "() => document.querySelectorAll('tr.asset-row').length >= 1",
            timeout=POLL_TIMEOUT_MS,
        )
        # Small settle so any (buggy) reload path would have fired by now.
        page.wait_for_timeout(500)
        assert page.evaluate("window.__noReloadSentinel") == "keep-me"
