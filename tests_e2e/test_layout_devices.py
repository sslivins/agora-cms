"""Layout/overflow regression tests for /devices — issue #444 (PR2).

Mirrors :mod:`tests_e2e.test_layout_schedules` for the Devices page.
The Devices page renders adopted devices as table rows (one per
device) inside a "Devices" card, with a kebab menu in the last cell
of each row.  The same kebab-off-the-side-of-the-table class of bug
applies; this file extends the layout-regression coverage to it.

We deliberately skip the "Pending Devices", "Device Groups", and
"Ungrouped Devices" cards in this PR — they involve more elaborate
seeding (group creation + group-internal device rows) and the same
kebab popover is wired up identically, so the regression risk for
the main "Devices" table is covered by these assertions.  Follow-ups
can extend if needed.

See sslivins/agora-cms#444 for design rationale.
"""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e._layout import (
    assert_closed_kebabs_in_cells,
    assert_no_horizontal_overflow,
    assert_open_kebab_in_viewport,
)
from tests_e2e.conftest import run_async
from tests_e2e.fake_device import FakeDevice


# ── Long-but-realistic strings drive the worst-case row width ──
#
# Devices page columns (admin view, 8 cols):
#   Expand | Name | Status | Group | Default Asset | Profile |
#   Firmware | Actions
# Wide content lives in Name and Group; the dropdowns for Asset /
# Profile size to the longest option.
_LONG_DEVICE_NAME = "Lobby — Building 92 — North Wing — Hallway Display 04"
_LONG_GROUP_NAME = "Building 92 / North Wing / Hallway Display Cluster"


@pytest.fixture
def _devices_seed(api, ws_url):
    """Seed one adopted device with a width-stressing name + group.

    Robust to leftover state across runs:
      * if the device id already exists, just rename / re-adopt as needed;
      * group create tolerates 409 by reusing the existing one.
    """
    device_id = "layout-444-devices-1"

    # Register over WS so the CMS sees a real adoption candidate, then
    # adopt via API.
    async def _register():
        async with FakeDevice(device_id, ws_url) as dev:
            await dev.send_status()

    run_async(_register())
    # Adopt — tolerant of "already adopted" from a prior run.
    adopt = api.post(f"/api/devices/{device_id}/adopt")
    assert adopt.status_code in (200, 201, 409), adopt.text

    # Force the long display name regardless of prior state.
    api.patch(
        f"/api/devices/{device_id}",
        json={"name": _LONG_DEVICE_NAME},
    )

    # Group create — sidestep the duplicate-group 500 by GET'ing first.
    existing_groups = api.get("/api/devices/groups/").json()
    group = next(
        (g for g in existing_groups if g.get("name") == _LONG_GROUP_NAME),
        None,
    )
    if group is None:
        resp = api.post(
            "/api/devices/groups/",
            json={"name": _LONG_GROUP_NAME},
        )
        assert resp.status_code in (200, 201), resp.text
        group = resp.json()

    api.patch(
        f"/api/devices/{device_id}",
        json={"group_id": group["id"]},
    )

    return {"device_id": device_id, "group_id": group["id"]}


# ── Page-load helper ──

def _goto_devices(page: Page) -> None:
    page.goto("/devices")
    page.wait_for_load_state("domcontentloaded")
    # The kebab buttons in each row are appended client-side after the
    # device list JSON loads — wait for at least one to exist before
    # measuring.
    page.wait_for_function(
        """() => {
            const main = document.querySelector('.card');
            if (!main) return false;
            return main.querySelector('tbody .btn-kebab') !== null;
        }""",
        timeout=5000,
    )


# Three desktop viewports — same matrix as test_layout_schedules.
_VIEWPORTS = [
    pytest.param(1024, 768,  id="1024x768"),
    pytest.param(1366, 768,  id="1366x768"),
    pytest.param(1440, 900,  id="1440x900"),
]


@pytest.mark.e2e
class TestDevicesLayout:
    """Geometry assertions for the /devices page."""

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_no_overflow_devices_table(
        self, page: Page, _devices_seed, vw, vh,
    ):
        """Devices table must not push the page past the viewport and
        every closed kebab must stay inside its actions cell.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_devices(page)

        # The first .card on this page is "Devices" — anchor by the
        # heading to be explicit.
        devices_card = page.locator(".card", has_text="Devices").first
        expect(devices_card).to_be_visible()

        assert_no_horizontal_overflow(
            page, label=f"@{vw}x{vh} devices",
        )
        assert_closed_kebabs_in_cells(
            page,
            devices_card.locator("table"),
            label=f"@{vw}x{vh} devices",
        )

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_open_kebab_stays_in_viewport_devices(
        self, page: Page, _devices_seed, vw, vh,
    ):
        """Open kebab on the Devices table — menu must stay in viewport
        and anchor near the trigger.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_devices(page)

        devices_card = page.locator(".card", has_text="Devices").first
        first_kebab = devices_card.locator("tbody .btn-kebab").first
        expect(first_kebab).to_be_visible()

        assert_open_kebab_in_viewport(
            page, first_kebab, label=f"@{vw}x{vh} devices",
        )
