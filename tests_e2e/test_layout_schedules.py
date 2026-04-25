"""Layout/overflow regression tests for /schedules — issue #444.

Catches the class-of-bug we hit twice in a row:
  * Schedules → Expired Schedules: kebab visually overflows past the
    right edge of the table.
  * In general: tables/menus that sneak past the viewport on narrower
    desktops.

Three geometric assertions per (viewport × table) — see ``_layout.py``:
  1. ``assert_no_horizontal_overflow``        — page-level
  2. ``assert_closed_kebabs_in_cells``        — closed kebab in its cell
  3. ``assert_open_kebab_in_viewport``        — open menu in viewport,
                                                anchored near trigger

We deliberately keep this narrow: no clipped-text detector, no
overlap probe, no pixel-diff baselines (see the issue body for why).
"""

from datetime import datetime, timedelta, timezone

import pytest
from playwright.sync_api import Page, expect

from tests_e2e._layout import (
    assert_closed_kebabs_in_cells,
    assert_no_horizontal_overflow,
    assert_open_kebab_in_viewport,
)
from tests_e2e.conftest import run_async
from tests_e2e.fake_device import FakeDevice


# ── Fixture: seed an active + an expired schedule with width-stressing data ──
#
# What stresses table width on /schedules:
#   - long schedule name           (Name column)
#   - long asset display name      (Asset column)
#   - long group name              (Target column)
#   - long days-of-week / time     (Schedule column — rendered client-side)
# (NOT "many devices" — Target shows the *group* name, not a device list.)


# Long-but-realistic strings drive the worst-case row width.
_LONG_NAME = "Lobby — Marketing Loop (Daily, Wednesdays Excluded)"
_LONG_ASSET = "Q4-2026-marketing-promo-extended-cut-v3-final-FINAL.mp4"
_LONG_GROUP = "Building 92 / North Wing / Hallway Display Cluster"


@pytest.fixture
def _schedules_seed(api, ws_url):
    """Seed one active + one expired schedule using width-stressing names.

    Returns a dict with the created entity ids so tests can clean up
    or reference them. Cleans up any existing schedules first to keep
    the fixture deterministic across test ordering.
    """
    # Wipe schedules from any prior test that ran in the same session.
    for s in api.get("/api/schedules").json():
        api.delete(f"/api/schedules/{s['id']}")

    # Register + adopt a device so it can be a schedule target.
    device_id = "layout-444-device"

    async def _register():
        async with FakeDevice(device_id, ws_url) as dev:
            await dev.send_status()

    run_async(_register())
    api.post(f"/api/devices/{device_id}/adopt")

    group = api.post(
        "/api/devices/groups/", json={"name": _LONG_GROUP},
    ).json()
    api.patch(f"/api/devices/{device_id}", json={"group_id": group["id"]})

    # Asset.
    assets = api.get("/api/assets").json()
    if not assets:
        api.create_asset(_LONG_ASSET)
        assets = api.get("/api/assets").json()
        if not assets:
            pytest.skip("Could not create test asset")
    asset = assets[0]
    # If asset already exists from a prior test, give it a long display
    # name so the Asset column is forced wider.
    api.patch(f"/api/assets/{asset['id']}", json={"display_name": _LONG_ASSET})

    # Active schedule with width-stressing name.
    active = api.post("/api/schedules", json={
        "name": _LONG_NAME,
        "group_id": group["id"],
        "asset_id": asset["id"],
        "start_time": "08:00",
        "end_time": "20:00",
        "days_of_week": [1, 2, 3, 4, 5, 6, 7],
    })
    assert active.status_code == 201, active.text
    active_id = active.json()["id"]

    # Expired schedule — comfortably in the past so no boundary flake.
    seven_days_ago = (
        datetime.now(timezone.utc) - timedelta(days=7)
    ).strftime("%Y-%m-%dT00:00:00Z")
    fourteen_days_ago = (
        datetime.now(timezone.utc) - timedelta(days=14)
    ).strftime("%Y-%m-%dT00:00:00Z")
    expired = api.post("/api/schedules", json={
        "name": _LONG_NAME + " (archived)",
        "group_id": group["id"],
        "asset_id": asset["id"],
        "start_time": "09:00",
        "end_time": "17:00",
        "start_date": fourteen_days_ago,
        "end_date": seven_days_ago,
    })
    assert expired.status_code == 201, expired.text

    return {
        "device_id": device_id,
        "group_id": group["id"],
        "asset_id": asset["id"],
        "active_id": active_id,
        "expired_id": expired.json()["id"],
    }


# ── Page-load helpers ──

def _goto_schedules(page: Page) -> None:
    page.goto("/schedules")
    page.wait_for_load_state("domcontentloaded")
    # ``schedule-desc`` cells are populated by JS on DOMContentLoaded;
    # wait until that JSON-driven render has actually written content
    # before measuring widths/positions.
    page.wait_for_function(
        """() => {
            const cells = document.querySelectorAll('.table-schedules .schedule-desc');
            if (cells.length === 0) return true;
            return Array.from(cells).every(c => c.textContent.trim().length > 0);
        }""",
        timeout=5000,
    )


# ── Viewports ──
#
# Three desktop viewports (no mobile — operator-only product):
#   * 1024×768  — narrow laptop / split-screen window (where overflow
#                 bugs surface most often)
#   * 1366×768  — modal mid-laptop
#   * 1440×900  — common widescreen
# 1920×1080 is omitted: above the 1400px ``main`` cap, low signal.
_VIEWPORTS = [
    pytest.param(1024, 768,  id="1024x768"),
    pytest.param(1366, 768,  id="1366x768"),
    pytest.param(1440, 900,  id="1440x900"),
]


@pytest.mark.e2e
class TestSchedulesLayout:
    """Geometry assertions for the /schedules page."""

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_no_overflow_active_table(
        self, page: Page, _schedules_seed, vw, vh,
    ):
        """Active Schedules table must not push the page past the viewport
        and every closed kebab must stay inside its actions cell.

        Combined into one test per viewport rather than three separate
        tests to keep the suite well under the 30s budget — three
        assertions on the same loaded page is cheap.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_schedules(page)

        active_card = page.locator(".card", has_text="Active Schedules")
        expect(active_card).to_be_visible()

        assert_no_horizontal_overflow(
            page, label=f"@{vw}x{vh} active",
        )
        assert_closed_kebabs_in_cells(
            page,
            active_card.locator("table.table-schedules"),
            label=f"@{vw}x{vh} active",
        )

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_no_overflow_expired_table(
        self, page: Page, _schedules_seed, vw, vh,
    ):
        """Expired Schedules table — same assertions.

        This is the table where the kebab-off-edge bug was first
        noticed by an operator. We also re-check page-level overflow:
        the expired card pushes the whole page taller and could surface
        a different overflow path.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_schedules(page)

        expired_card = page.locator(".card", has_text="Expired Schedules")
        expect(expired_card).to_be_visible()

        assert_no_horizontal_overflow(
            page, label=f"@{vw}x{vh} expired",
        )
        assert_closed_kebabs_in_cells(
            page,
            expired_card.locator("table.table-schedules"),
            label=f"@{vw}x{vh} expired",
        )

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_open_kebab_stays_in_viewport_active(
        self, page: Page, _schedules_seed, vw, vh,
    ):
        """Open kebab on the active table — menu must stay in viewport
        and anchor near the trigger button."""
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_schedules(page)

        active_card = page.locator(".card", has_text="Active Schedules")
        first_kebab = active_card.locator(
            "tbody tr .btn-kebab",
        ).first
        expect(first_kebab).to_be_visible()

        assert_open_kebab_in_viewport(
            page, first_kebab, label=f"@{vw}x{vh} active",
        )

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_open_kebab_stays_in_viewport_expired(
        self, page: Page, _schedules_seed, vw, vh,
    ):
        """Open kebab on the expired table — same viewport assertion.

        This is where the visual bug originally surfaced — even if
        the closed-kebab cell-overflow check above already flags it,
        we also confirm the open menu doesn't fall off the viewport.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_schedules(page)

        expired_card = page.locator(".card", has_text="Expired Schedules")
        first_kebab = expired_card.locator(
            "tbody tr .btn-kebab",
        ).first
        expect(first_kebab).to_be_visible()

        assert_open_kebab_in_viewport(
            page, first_kebab, label=f"@{vw}x{vh} expired",
        )
