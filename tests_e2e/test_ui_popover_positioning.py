"""E2E regression tests for popover positioning (PR #362).

Both the kebab (⋮) action menu and the group-picker `+` popup use the
native HTML popover API. Bugs we're guarding against:

1. The `+` popup never moved off (0,0) because ``positionPopover()``
   couldn't find the invoker via ``[popovertarget]`` (the `+` buttons
   open via ``onclick="openGroupPopup(...)"`` and have no
   ``popovertarget`` attribute). Fixed with a ``.group-picker-wrap``
   sibling-lookup fallback.

2. The kebab flashed at (0,0) for one frame on every open because the
   browser paints the popover at its CSS default position before the
   ``toggle`` event fires. Fixed by parking both popovers off-screen
   (``top: -9999px; left: -9999px``) by default — the pre-positioned
   frame is now invisible.

These tests assert the popover ends up *near its invoker button*
after opening, which catches both the "stuck at (0,0)" failure
mode and any future positioning regressions.
"""

import pytest
from playwright.sync_api import Page, expect


def _create_group(api, name: str = "popover-position-test"):
    resp = api.post("/api/devices/groups/", json={"name": name})
    if resp.status_code not in (200, 201, 409):  # 409 = already exists from prior run
        raise AssertionError(f"Failed to create group: {resp.status_code} {resp.text}")


@pytest.mark.e2e
class TestPopoverPositioning:
    """Popovers must anchor near their invoker — never at viewport (0,0)."""

    def test_kebab_menu_opens_near_invoker_button(self, page: Page, e2e_server):
        """Kebab popover top/left must be within ~200px of its ⋮ button.

        Regression: before the fix, the kebab briefly painted at (0,0)
        on every open because the toggle event handler runs *after* the
        first paint. We park the popover off-screen by default so that
        first paint is invisible — but more importantly, this test
        catches a future regression where positionKebab() stops running
        entirely (popover would then stay at -9999px).
        """
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has=page.locator(".badge-builtin")).first
        kebab_btn = row.locator(".btn-kebab")

        kebab_btn.click()
        menu = page.locator(".kebab-menu:popover-open")
        expect(menu).to_have_count(1)

        btn_box = kebab_btn.bounding_box()
        menu_box = menu.bounding_box()
        assert btn_box is not None and menu_box is not None

        # Menu should be on-screen (not parked at -9999px) — the off-screen
        # default is the "before positioning" state. If positionKebab()
        # silently fails, this catches it.
        assert menu_box["x"] >= 0, (
            f"Kebab menu left={menu_box['x']} is off-screen. "
            f"positionKebab() likely didn't run — see app.js positionPopover()."
        )
        assert menu_box["y"] >= 0, (
            f"Kebab menu top={menu_box['y']} is off-screen."
        )

        # Menu should be reasonably close to its invoker button (within
        # 250px both axes — generous to avoid layout-flake while still
        # catching the (0,0) bug, where distance from a typical row
        # kebab is hundreds of px).
        dx = abs(menu_box["x"] - btn_box["x"])
        dy = abs(menu_box["y"] - btn_box["y"])
        assert dx < 250 and dy < 250, (
            f"Kebab menu at ({menu_box['x']}, {menu_box['y']}) is too far from "
            f"its invoker at ({btn_box['x']}, {btn_box['y']}) — "
            f"dx={dx}, dy={dy}. Likely positioning regression."
        )

    def test_group_plus_popup_opens_near_invoker_button(
        self, page: Page, api, e2e_server,
    ):
        """Group `+` popup must position near its invoker button.

        Regression (PR #362 root cause): ``positionPopover()`` looked up
        the invoker via ``[popovertarget="X"]`` only, but `+` buttons
        use ``onclick="openGroupPopup('X')"`` instead — so the lookup
        returned null and the popup was glued to (0,0). The fix adds
        a ``.group-picker-wrap`` sibling fallback. If that fallback
        ever breaks, this test fails.
        """
        # Need at least one group so the upload `+` button is enabled.
        _create_group(api)

        page.goto("/assets")
        page.wait_for_selector("#upload-form")

        plus_btn = page.locator("#upload-groups-badges .btn-add-group")
        expect(plus_btn).to_be_visible()
        expect(plus_btn).to_be_enabled()

        plus_btn.click()
        popup = page.locator("#upload-group-popup")
        # Wait for popover-open state; CSS toggles display:flex via :popover-open.
        page.wait_for_function(
            "el => el.matches(':popover-open')",
            arg=popup.element_handle(),
            timeout=2000,
        )

        btn_box = plus_btn.bounding_box()
        popup_box = popup.bounding_box()
        assert btn_box is not None and popup_box is not None

        # Catch the original bug: popup stuck at top-left of viewport.
        assert popup_box["x"] >= 0, (
            f"Group popup left={popup_box['x']} is off-screen. "
            f"positionGroupPopup() didn't run — see the .group-picker-wrap "
            f"fallback in app.js positionPopover()."
        )
        # The original bug parked the popup at (0,0); button typically
        # sits hundreds of px down/right of that on /assets.
        assert popup_box["y"] >= 0
        assert not (popup_box["x"] < 5 and popup_box["y"] < 5), (
            f"Group popup is glued to viewport (0,0) — this is the exact "
            f"failure mode of the [popovertarget] lookup bug. Verify the "
            f".group-picker-wrap fallback in positionPopover() is intact."
        )

        # Popup should be reasonably close to the `+` button.
        dx = abs(popup_box["x"] - btn_box["x"])
        dy = abs(popup_box["y"] - btn_box["y"])
        assert dx < 250 and dy < 250, (
            f"Group popup at ({popup_box['x']}, {popup_box['y']}) is too far "
            f"from its `+` button at ({btn_box['x']}, {btn_box['y']}) — "
            f"dx={dx}, dy={dy}. Likely positioning regression."
        )
