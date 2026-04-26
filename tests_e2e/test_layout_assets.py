"""Layout/overflow regression tests for /assets — issue #444 (PR4).

Mirrors :mod:`tests_e2e.test_layout_users` for the Assets page.
The Library card on ``/assets`` renders one ``<tr class="asset-row">``
per asset with a kebab menu in the last cell, and each row toggles
a hidden detail row on click. The same kebab-off-the-side-of-the-table
class of bug applies; this file extends the layout-regression coverage
to it.

We deliberately scope to the **Library** card here. The Transcoding
Queue card renders a different table and is admin-only with no kebab,
so the table-cell-clipping failure mode does not apply (and it already
sits inside a ``.table-wrap``).

The seeded asset is created as a *webpage* asset (no upload, no
transcode), with a long ``display_name`` patched in afterwards — the
``asset_row`` macro renders ``a.display_name or a.original_filename or
a.filename``, so the long display_name drives row width regardless of
asset type.

See sslivins/agora-cms#444 for design rationale.
"""

import uuid

import pytest
from playwright.sync_api import Page, expect

from tests_e2e._layout import (
    assert_closed_kebabs_in_cells,
    assert_no_horizontal_overflow,
    assert_open_kebab_in_viewport,
)


# ── Long-but-realistic strings drive the worst-case row width ──
#
# Assets page columns (admin view, 8 cols):
#   ▶ | Name | Type | Scope | Size | Status | Duration | Actions
# Wide content lives in Name and (when many groups) Scope.
_LAYOUT_DISPLAY_NAME = (
    "Layout #444 — Building 92 — North Wing — Hallway Display"
    " Welcome Loop (final cut, 4K, 60fps)"
)


@pytest.fixture
def _asset_seed(api):
    """Seed (or re-seed) one webpage asset with a width-stressing
    display name. Robust to leftover state across runs.

    Returns ``{"id": <uuid>, "display_name": <str>}``.
    """
    # Webpage assets create instantly (no upload / no transcode), and
    # `asset_row` renders display_name first when set — so this drives
    # the long-row layout case without needing a multipart upload.
    unique = f"layout-444-assets-{uuid.uuid4().hex[:10]}"
    create_resp = api.post(
        "/api/assets/webpage",
        json={
            "url": f"https://example.com/{unique}",
            "name": unique,
        },
    )
    assert create_resp.status_code == 201, (
        f"create webpage asset: {create_resp.status_code} {create_resp.text}"
    )
    asset_id = create_resp.json()["id"]

    # Force the long display name regardless of the create-derived name.
    patch_resp = api.patch(
        f"/api/assets/{asset_id}",
        json={"display_name": _LAYOUT_DISPLAY_NAME},
    )
    assert patch_resp.status_code == 200, (
        f"patch display_name: {patch_resp.status_code} {patch_resp.text}"
    )

    return {"id": asset_id, "display_name": _LAYOUT_DISPLAY_NAME}


# ── Page-load helper ──

def _goto_assets(page: Page, asset_id: str) -> None:
    page.goto("/assets")
    page.wait_for_load_state("domcontentloaded")
    # The seeded asset's row must be present before we measure.
    page.wait_for_selector(
        f'tr.asset-row[data-asset-id="{asset_id}"]',
        timeout=5000,
    )


# Three desktop viewports — same matrix as the other layout tests.
_VIEWPORTS = [
    pytest.param(1024, 768, id="1024x768"),
    pytest.param(1366, 768, id="1366x768"),
    pytest.param(1440, 900, id="1440x900"),
]


@pytest.mark.e2e
class TestAssetsLayout:
    """Geometry assertions for the /assets Library card."""

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_no_overflow_assets_table(
        self, page: Page, _asset_seed, vw, vh,
    ):
        """Library table must not push the page past the viewport and
        every closed kebab must stay inside its actions cell.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_assets(page, _asset_seed["id"])

        # Anchor by the card containing the seeded row by *id* — the
        # display_name is unique per fixture run but text-based filters
        # are fragile when prior runs leave similar names behind.
        library_card = page.locator(".card").filter(
            has=page.locator(
                f'tr.asset-row[data-asset-id="{_asset_seed["id"]}"]'
            ),
        ).first
        expect(library_card).to_be_visible()

        assert_no_horizontal_overflow(
            page, label=f"@{vw}x{vh} assets",
        )
        assert_closed_kebabs_in_cells(
            page,
            library_card.locator("table").first,
            label=f"@{vw}x{vh} assets",
        )

    @pytest.mark.parametrize("vw,vh", _VIEWPORTS)
    def test_open_kebab_stays_in_viewport_assets(
        self, page: Page, _asset_seed, vw, vh,
    ):
        """Open the kebab on the seeded asset's row — menu must stay
        in viewport and anchor near the trigger.
        """
        page.set_viewport_size({"width": vw, "height": vh})
        _goto_assets(page, _asset_seed["id"])

        # Target the seeded row by id so we measure the long-name row,
        # not whatever asset happens to be first alphabetically.
        target_row = page.locator(
            f'tr.asset-row[data-asset-id="{_asset_seed["id"]}"]'
        ).first
        kebab = target_row.locator(".btn-kebab").first
        expect(kebab).to_be_visible()

        assert_open_kebab_in_viewport(
            page, kebab, label=f"@{vw}x{vh} assets",
        )
