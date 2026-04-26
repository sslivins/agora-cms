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
    or reference them.

    Robust to leftover state from prior tests in the same session and
    re-runs of parametrized cases:

    * any schedule whose name starts with our long-name prefix is
      deleted up-front so we don't trip the API's "overlapping time on
      the same target at same priority" 409;
    * group creation tolerates 409 by GET'ing the existing one;
    * both schedules are created at ``priority=99`` so they cannot
      conflict with anything left over at priority 0.
    """
    # Wipe any of our previously-created schedules so a leftover at the
    # same priority/target doesn't trigger the API's overlap check.
    for s in api.get("/api/schedules").json():
        if s.get("name", "").startswith(_LONG_NAME):
            api.delete(f"/api/schedules/{s['id']}")

    # Register + adopt a device so it can be a schedule target.
    device_id = "layout-444-device"

    async def _register():
        async with FakeDevice(device_id, ws_url) as dev:
            await dev.send_status()

    run_async(_register())
    api.post(f"/api/devices/{device_id}/adopt")

    # Group create — sidestep the duplicate-group 500 by GET'ing first.
    # The CMS POST raises IntegrityError on duplicate name (returns 500
    # rather than 409), and there's no upsert endpoint, so we reuse a
    # pre-existing group whenever possible.
    existing_groups = api.get("/api/devices/groups/").json()
    group = next(
        (g for g in existing_groups if g.get("name") == _LONG_GROUP), None,
    )
    if group is None:
        group_resp = api.post(
            "/api/devices/groups/", json={"name": _LONG_GROUP},
        )
        assert group_resp.status_code in (200, 201), group_resp.text
        group = group_resp.json()
    api.patch(f"/api/devices/{device_id}", json={"group_id": group["id"]})

    # Asset.  Always create a fresh ready asset rather than reusing
    # ``assets[0]``: prior tests may have left behind assets that the
    # schedules API rejects with HTTP 422 — either webpage assets
    # (Pi-5-only validation against our seeded device) or assets whose
    # variants are still PENDING/PROCESSING (transcode-readiness gate
    # from #312). ``api.create_asset`` defaults to ``ready=True`` and
    # marks all variants READY in the DB so the schedule POST succeeds
    # regardless of what's been created earlier in the same session.
    asset_resp = api.create_asset(_LONG_ASSET)
    if asset_resp.status_code != 201:
        pytest.skip(f"Could not create test asset: {asset_resp.status_code} {asset_resp.text}")
    asset = asset_resp.json()
    api.patch(f"/api/assets/{asset['id']}", json={"display_name": _LONG_ASSET})

    # Active schedule with width-stressing name.  Distinct priorities
    # for active vs expired keep them out of the overlap check (the
    # check compares only same-priority schedules on the same target;
    # date_range is ignored, so 09–17 expired would otherwise conflict
    # with 08–20 active even though they're temporally disjoint).
    active = api.post("/api/schedules", json={
        "name": _LONG_NAME,
        "group_id": group["id"],
        "asset_id": asset["id"],
        "start_time": "08:00",
        "end_time": "20:00",
        "days_of_week": [1, 2, 3, 4, 5, 6, 7],
        "priority": 99,
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
        "priority": 98,
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
