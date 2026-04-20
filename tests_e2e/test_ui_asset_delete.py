"""E2E regression for issue #87 — no full-page reload on asset delete.

Guards the UX win from PR #367: clicking Delete in an asset's kebab
menu removes just that asset's row (and its expanded-detail row)
without navigating away, so scroll position and any other expanded
rows stay intact.

If someone re-introduces a ``location.reload()`` in ``deleteAsset``,
the ``page.url`` and ``framenavigated`` assertions here will catch it.
"""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import click_row_action


@pytest.mark.e2e
class TestAssetDeleteNoReload:
    def test_delete_asset_removes_row_without_navigation(
        self, page: Page, api, e2e_server
    ):
        # Create two assets so we can tell the "remove one row" change from a
        # "reload the whole table" change — after the delete, the other row
        # should still be present (and it demonstrably came from the original
        # page load, not a re-fetch).
        keep = api.create_asset(filename="e2e-keep-reload.mp4").json()
        target = api.create_asset(filename="e2e-delete-me.mp4").json()

        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")

        target_row = page.locator(f'tr.asset-row[data-asset-id="{target["id"]}"]')
        keep_row = page.locator(f'tr.asset-row[data-asset-id="{keep["id"]}"]')
        expect(target_row).to_have_count(1)
        expect(keep_row).to_have_count(1)

        # Stamp the page so we can verify after the delete that no reload
        # happened. If the DOM was re-rendered by a full navigation this
        # custom property would be gone.
        page.evaluate("window.__reloadSentinel = 'before-delete';")

        # Also watch for any framenavigated event during the delete — a
        # belt-and-suspenders check alongside the sentinel.
        navigations: list[str] = []
        page.on("framenavigated", lambda frame: navigations.append(frame.url))

        url_before = page.url

        click_row_action(target_row, "Delete")

        # The confirm modal from showConfirm() — click "Confirm" to proceed.
        page.get_by_role("button", name="Confirm").click()

        # Target row disappears, kept row is still there.
        expect(target_row).to_have_count(0, timeout=5000)
        expect(keep_row).to_have_count(1)

        # Detail row (if any) for the deleted asset is also gone.
        expect(
            page.locator(f'tr.asset-detail[data-detail-for="{target["id"]}"]')
        ).to_have_count(0)

        # No navigation happened — URL is unchanged, the sentinel still
        # lives on window, and no framenavigated event fired to this page's
        # frame during the delete.
        assert page.url == url_before, (
            f"asset delete must not navigate; url changed to {page.url!r}"
        )
        assert page.evaluate("window.__reloadSentinel") == "before-delete", (
            "__reloadSentinel was cleared — looks like a full-page reload happened"
        )
        assert navigations == [], (
            f"asset delete fired navigation event(s): {navigations!r}"
        )
