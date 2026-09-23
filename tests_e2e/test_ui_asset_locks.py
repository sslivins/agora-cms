"""E2E coverage for the per-asset interaction lock.

Two bugs are covered here.

The first is that ``isModalOpen()`` never worked. It tested
``offsetParent !== null``, and a ``position: fixed`` element's offsetParent
is always null, so it reported "no modal open" with a modal on screen. Every
caller used it as a poll guard, on five pages, so none of those guards ever
fired. Because it never fired, no test ever noticed.

The second is the guard's shape. Pausing the entire library because one
dialog is open means the user opens a modal on one asset and every other row
goes stale. The page now skips only the assets the user is interacting with,
via ``_isAssetLocked``.

These drive ``updateLiveAssets`` directly with synthetic payloads rather than
waiting on the 5s poller, which keeps the assertions deterministic. The
payload always carries a *changed* value, so a row that fails to lock
visibly updates -- that's what makes a passing assertion meaningful rather
than an artefact of nothing having happened.
"""

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
class TestModalGuardIsAlive:
    """Regression for the offsetParent bug itself."""

    def test_is_modal_open_sees_a_visible_fixed_overlay(
        self, page: Page, api, e2e_server
    ):
        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_function("() => typeof window.isModalOpen === 'function'")

        assert page.evaluate("window.isModalOpen()") is False

        # Built to match the real thing: .modal-overlay is position:fixed,
        # which is precisely what defeated the old check.
        state = page.evaluate(
            """() => {
                const o = document.createElement('div');
                o.className = 'modal-overlay';
                o.style.cssText =
                    'position:fixed;inset:0;display:flex;';
                document.body.appendChild(o);
                return {
                    offsetParentIsNull: o.offsetParent === null,
                    reallyVisible: o.checkVisibility(),
                    isModalOpen: window.isModalOpen(),
                };
            }"""
        )

        # Guards the premise: if this ever goes false the overlay stopped
        # being fixed and the test no longer exercises the bug.
        assert state["offsetParentIsNull"] is True
        assert state["reallyVisible"] is True
        assert state["isModalOpen"] is True

    def test_is_modal_open_is_false_again_once_hidden(
        self, page: Page, api, e2e_server
    ):
        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_function("() => typeof window.isModalOpen === 'function'")

        assert page.evaluate(
            """() => {
                const o = document.createElement('div');
                o.className = 'modal-overlay';
                o.style.cssText = 'position:fixed;inset:0;display:none;';
                document.body.appendChild(o);
                return window.isModalOpen();
            }"""
        ) is False


@pytest.mark.e2e
class TestPerAssetLock:
    def _goto_assets(self, page: Page):
        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")
        page.wait_for_function("() => typeof window._isAssetLocked === 'function'")
        page.wait_for_function("() => typeof window.updateLiveAssets === 'function'")

    @staticmethod
    def _payload(asset_id):
        """A variant badge value that differs from the freshly-rendered row."""
        return [{
            "id": asset_id,
            "asset_type": "video",
            "variant_ready": 7,
            "variant_total": 7,
            "variants": [],
        }]

    def test_an_idle_row_is_not_locked_and_does_update(
        self, page: Page, api, e2e_server
    ):
        """Precondition. Without this the lock tests could pass vacuously --
        a row that never updates under any circumstances would satisfy them."""
        asset = api.create_asset(filename="e2e-lock-idle.mp4").json()
        self._goto_assets(page)

        cell = f'[data-live-variants="{asset["id"]}"]'
        before = page.locator(cell).inner_html()

        assert page.evaluate("id => window._isAssetLocked(id)", asset["id"]) is False
        page.evaluate("p => window.updateLiveAssets(p)", self._payload(asset["id"]))

        assert page.locator(cell).inner_html() != before

    def test_focus_inside_a_row_locks_it(self, page: Page, api, e2e_server):
        asset = api.create_asset(filename="e2e-lock-focus.mp4").json()
        self._goto_assets(page)

        cell = f'[data-live-variants="{asset["id"]}"]'
        before = page.locator(cell).inner_html()

        page.evaluate(
            """id => {
                const row = document.querySelector(
                    'tr.asset-row[data-asset-id="' + id + '"]');
                const input = document.createElement('input');
                row.querySelector('td').appendChild(input);
                input.focus();
            }""",
            asset["id"],
        )

        assert page.evaluate("id => window._isAssetLocked(id)", asset["id"]) is True
        page.evaluate("p => window.updateLiveAssets(p)", self._payload(asset["id"]))

        assert page.locator(cell).inner_html() == before

    def test_playing_audio_locks_the_row(self, page: Page, api, e2e_server):
        """Swapping the element mid-playback restarts the clip."""
        asset = api.create_asset(filename="e2e-lock-audio.mp4").json()
        self._goto_assets(page)

        cell = f'[data-live-variants="{asset["id"]}"]'
        before = page.locator(cell).inner_html()

        # Stubbed rather than really playing: a real <audio> needs a decodable
        # source and an autoplay allowance, neither of which this is about.
        page.evaluate(
            """id => {
                const row = document.querySelector(
                    'tr.asset-row[data-asset-id="' + id + '"]');
                const a = document.createElement('audio');
                Object.defineProperty(a, 'paused', {value: false});
                Object.defineProperty(a, 'ended', {value: false});
                Object.defineProperty(a, 'currentTime', {value: 3});
                row.querySelector('td').appendChild(a);
            }""",
            asset["id"],
        )

        assert page.evaluate("id => window._isAssetLocked(id)", asset["id"]) is True
        page.evaluate("p => window.updateLiveAssets(p)", self._payload(asset["id"]))

        assert page.locator(cell).inner_html() == before

    def test_a_lock_on_one_asset_does_not_freeze_the_others(
        self, page: Page, api, e2e_server
    ):
        """The whole point of replacing the global pause."""
        locked = api.create_asset(filename="e2e-lock-busy.mp4").json()
        free = api.create_asset(filename="e2e-lock-free.mp4").json()
        self._goto_assets(page)

        locked_cell = f'[data-live-variants="{locked["id"]}"]'
        free_cell = f'[data-live-variants="{free["id"]}"]'
        locked_before = page.locator(locked_cell).inner_html()
        free_before = page.locator(free_cell).inner_html()

        page.evaluate(
            """id => {
                const row = document.querySelector(
                    'tr.asset-row[data-asset-id="' + id + '"]');
                const input = document.createElement('input');
                row.querySelector('td').appendChild(input);
                input.focus();
            }""",
            locked["id"],
        )

        page.evaluate(
            "p => window.updateLiveAssets(p)",
            self._payload(locked["id"]) + self._payload(free["id"]),
        )

        assert page.locator(locked_cell).inner_html() == locked_before
        assert page.locator(free_cell).inner_html() != free_before

    def test_a_deleted_asset_is_removed_even_while_locked(
        self, page: Page, api, e2e_server
    ):
        """Deletions stay unconditional. Leaving a focused row on screen for
        an asset the server no longer has would let the user act on something
        that doesn't exist."""
        keep = api.create_asset(filename="e2e-lock-del-keep.mp4").json()
        doomed = api.create_asset(filename="e2e-lock-del-gone.mp4").json()
        self._goto_assets(page)

        page.evaluate(
            """id => {
                const row = document.querySelector(
                    'tr.asset-row[data-asset-id="' + id + '"]');
                const input = document.createElement('input');
                row.querySelector('td').appendChild(input);
                input.focus();
            }""",
            doomed["id"],
        )
        assert page.evaluate("id => window._isAssetLocked(id)", doomed["id"]) is True

        page.evaluate(
            "async ids => { await window._applyMembership(ids, 50); }",
            [keep["id"]],
        )

        expect(
            page.locator(f'tr.asset-row[data-asset-id="{doomed["id"]}"]')
        ).to_have_count(0)
        expect(
            page.locator(f'tr.asset-row[data-asset-id="{keep["id"]}"]')
        ).to_have_count(1)
