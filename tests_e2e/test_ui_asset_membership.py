"""E2E wiring check for the asset library's membership reconciler.

The decision logic lives in cms/static/asset_reconcile.js and is unit-tested
in tests/test_asset_reconcile_js.py. That proves the function is *correct*;
it cannot prove the page *calls it correctly*. Wiring is exactly where the
previous regression landed -- the logic was fine, the seed value it was fed
was not -- so this drives the real page's real entry points with synthetic
inputs and asserts the resulting DOM.

Synthetic inputs rather than real mutations plus a 5s wait: it makes the
assertions deterministic and keeps the test off the poller's timer.
"""

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
class TestAssetMembershipWiring:
    def _goto_assets(self, page: Page):
        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_function(
            "() => typeof window.computeAssetReconcile === 'function'"
        )
        page.wait_for_function(
            "() => typeof window._applyMembership === 'function'"
        )

    def test_reconciler_module_is_loaded_on_the_page(self, page: Page, api, e2e_server):
        """A missing <script> tag would leave the poller silently inert."""
        api.create_asset(filename="e2e-wire-present.mp4").json()
        self._goto_assets(page)

        assert page.evaluate(
            "typeof window.computeAssetReconcile === 'function'"
        ) is True

    def test_removes_a_row_the_server_no_longer_lists(
        self, page: Page, api, e2e_server
    ):
        keep = api.create_asset(filename="e2e-wire-keep.mp4").json()
        gone = api.create_asset(filename="e2e-wire-gone.mp4").json()

        self._goto_assets(page)
        expect(page.locator(f'tr.asset-row[data-asset-id="{gone["id"]}"]')).to_have_count(1)

        page.evaluate(
            "async (ids) => { await window._applyMembership(ids, 50); }",
            [keep["id"]],
        )

        expect(page.locator(f'tr.asset-row[data-asset-id="{gone["id"]}"]')).to_have_count(0)
        expect(page.locator(f'tr.asset-row[data-asset-id="{keep["id"]}"]')).to_have_count(1)

    def test_inserts_a_row_that_appeared_at_the_top(
        self, page: Page, api, e2e_server
    ):
        existing = api.create_asset(filename="e2e-wire-existing.mp4").json()

        self._goto_assets(page)

        # Created after render, so the page has never seen it -- the same
        # situation as an upload from another replica.
        fresh = api.create_asset(filename="e2e-wire-fresh.mp4").json()
        expect(page.locator(f'tr.asset-row[data-asset-id="{fresh["id"]}"]')).to_have_count(0)

        ids = page.evaluate(
            """async (args) => {
                const dom = Array.from(
                    document.querySelectorAll('tr.asset-row[data-asset-id]')
                ).map(r => r.dataset.assetId);
                await window._applyMembership([args.fresh].concat(dom), 50);
                return Array.from(
                    document.querySelectorAll('tr.asset-row[data-asset-id]')
                ).map(r => r.dataset.assetId);
            }""",
            {"fresh": fresh["id"]},
        )

        assert ids[0] == fresh["id"], "newest asset should land at the top"
        assert existing["id"] in ids

    def test_does_not_insert_rows_below_the_loaded_window(
        self, page: Page, api, e2e_server
    ):
        """The pagination guard, checked against the live page.

        Ids the user has not scrolled to must not trigger a row fetch each --
        that was several hundred requests on a large library.
        """
        api.create_asset(filename="e2e-wire-window.mp4").json()
        self._goto_assets(page)

        requests: list[str] = []
        page.on(
            "request",
            lambda r: requests.append(r.url) if "/row" in r.url else None,
        )

        added = page.evaluate(
            """async () => {
                const dom = Array.from(
                    document.querySelectorAll('tr.asset-row[data-asset-id]')
                ).map(r => r.dataset.assetId);
                const before = dom.length;
                // Real ids first, then 200 ids the user has not scrolled to.
                const tail = Array.from({length: 200}, (_, i) => 'unscrolled-' + i);
                await window._applyMembership(dom.concat(tail), 50);
                return document.querySelectorAll(
                    'tr.asset-row[data-asset-id]').length - before;
            }"""
        )

        assert added == 0, "assets below the loaded window must not be inserted"
        assert requests == [], "no row fetches should be issued"

    def test_vanished_rows_are_dropped_from_the_scoped_payload(
        self, page: Page, api, e2e_server
    ):
        """Deletions are detected without any extra request: an id we asked
        about that does not come back is gone."""
        keep = api.create_asset(filename="e2e-wire-vanish-keep.mp4").json()
        gone = api.create_asset(filename="e2e-wire-vanish-gone.mp4").json()

        self._goto_assets(page)
        expect(page.locator(f'tr.asset-row[data-asset-id="{gone["id"]}"]')).to_have_count(1)

        page.evaluate(
            """(args) => {
                window._removeVanishedRows(
                    [args.keep, args.gone], [{ id: args.keep }]);
            }""",
            {"keep": keep["id"], "gone": gone["id"]},
        )

        expect(page.locator(f'tr.asset-row[data-asset-id="{gone["id"]}"]')).to_have_count(0)
        expect(page.locator(f'tr.asset-row[data-asset-id="{keep["id"]}"]')).to_have_count(1)

    def test_grid_card_is_removed_alongside_the_row(
        self, page: Page, api, e2e_server
    ):
        """The old scope-change path removed the row but left the grid card,
        so toggling to grid view resurrected deleted assets."""
        keep = api.create_asset(filename="e2e-wire-grid-keep.mp4").json()
        gone = api.create_asset(filename="e2e-wire-grid-gone.mp4").json()

        self._goto_assets(page)

        page.evaluate(
            "async (ids) => { await window._applyMembership(ids, 50); }",
            [keep["id"]],
        )

        expect(page.locator(f'[data-grid-card="{gone["id"]}"]')).to_have_count(0)
