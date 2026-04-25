"""Layout/overflow regression tests for /dashboard — issue #444 (PR3).

Mirrors :mod:`tests_e2e.test_layout_devices` for the Dashboard page.
``/dashboard`` renders a server-rendered "Pending Devices" table when
there are devices that have registered but not been adopted, with a
kebab menu in the last cell of each row.  The same
"kebab-off-the-side-of-the-table" class of bug applies.

Other tables on /dashboard (Currently Playing, Upcoming, Recent
Activity) do not have kebab menus, so they are out of scope for this
file.

We seed a pending device whose row will appear as **Pending
(Offline)** — ``FakeDevice`` disconnects when the ``async with`` block
exits, but the dashboard view still renders the row because it queries
``DeviceStatus.PENDING`` from the database before annotating
liveness.  See ``cms/ui.py``.

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


# Use a unique device id so a leftover row from a previous run with a
# stale (short) name does not mask the width-stress signal.  The
# fixture deletes any prior row with this id before re-registering.
_LAYOUT_DEVICE_ID = "layout-444-dashboard-1"
_LONG_DEVICE_NAME = (
    "Lobby Display 14 — Building 92 — North Wing — Hallway Cluster"
)


@pytest.fixture
def _dashboard_pending_seed(api, ws_url):
    """Seed exactly one **pending** device with a width-stressing
    name.  Robust to leftover rows across runs.
    """
    # Wipe any prior row so the WS register starts from a clean slate
    # — the register handler does not refresh ``name`` for an existing
    # row when ``device_name_custom`` is False.
    api.delete(f"/api/devices/{_LAYOUT_DEVICE_ID}")

    async def _register():
        async with FakeDevice(_LAYOUT_DEVICE_ID, ws_url) as dev:
            await dev.send_status()

    run_async(_register())

    # Force the long display name on the now-pending row.
    api.patch(
        f"/api/devices/{_LAYOUT_DEVICE_ID}",
        json={"name": _LONG_DEVICE_NAME},
    )

    return {"device_id": _LAYOUT_DEVICE_ID, "name": _LONG_DEVICE_NAME}


# ── Page-load helper ──

def _goto_dashboard(page: Page, target_name: str) -> None:
    page.goto("/dashboard")
    page.wait_for_load_state("domcontentloaded")
    # Wait for the seeded pending row to be present before measuring.
    page.wait_for_function(
        """(name) => {
            const rows = document.querySelectorAll('tbody tr');
            for (const r of rows) {
                if (r.textContent && r.textContent.includes(name)) return true;
            }
            return false;
        }""",
        arg=target_name,
        timeout=5000,
    )


_VIEWPORTS = [
    pytest.param(1024, 768, id="1024x768"),
    pytest.param(1366, 768, id="1366x768"),
    pytest.param(1440, 900, id="1440x900"),
]


@pytest.mark.e2e
class TestDashboardLayout:
    """Geometry assertions for the /dashboard page."""

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_no_overflow_pending_devices_table(
        self, page: Page, _dashboard_pending_seed, vw, vh,
    ):
        """Pending Devices table must not push the page past the
        viewport and every closed kebab must stay inside its actions
        cell.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_dashboard(page, _dashboard_pending_seed["name"])

        pending_card = page.locator(
            ".card", has_text="Pending Devices",
        ).first
        expect(pending_card).to_be_visible()

        assert_no_horizontal_overflow(
            page, label=f"@{vw}x{vh} dashboard",
        )
        assert_closed_kebabs_in_cells(
            page,
            pending_card.locator("table").first,
            label=f"@{vw}x{vh} dashboard pending",
        )

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_open_kebab_stays_in_viewport_dashboard(
        self, page: Page, _dashboard_pending_seed, vw, vh,
    ):
        """Open the kebab on the seeded pending device row — menu must
        stay in viewport and anchor near the trigger.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_dashboard(page, _dashboard_pending_seed["name"])

        pending_card = page.locator(
            ".card", has_text="Pending Devices",
        ).first
        # Target the seeded row explicitly — leftover pending rows
        # from other tests would otherwise reorder ``.first``.
        target_row = pending_card.locator(
            "tbody tr", has_text=_dashboard_pending_seed["name"],
        ).first
        kebab = target_row.locator(".btn-kebab").first
        expect(kebab).to_be_visible()

        assert_open_kebab_in_viewport(
            page, kebab, label=f"@{vw}x{vh} dashboard",
        )
