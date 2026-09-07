"""Playwright coverage for asset tag chips and the shared "+ tag" picker.

Asset tags and device tags are different tables with different endpoints, but
since PR #882 they share one presentation: the ``tag_chips()`` macro plus the
``TagPicker`` component in ``app.js``. This file is the assets-side half of
that contract — ``test_ui_device_tags.py`` covers the device side. Between
them, a change to the shared component cannot silently break one consumer
while leaving the other green.
"""

import pytest
from playwright.sync_api import Page, expect


def _make_tag(api, name, color="#3366cc"):
    """Create a tag and return it. The API normalises names, so callers must
    assert against the stored name rather than what they asked for."""
    resp = api.post("/api/tags", json={"name": name, "color": color})
    assert resp.status_code in (200, 201), f"tag create failed: {resp.text}"
    return resp.json()


def _tag_box(page: Page, asset_id: str):
    return page.locator(
        f'.tag-chips[data-tag-scope="asset"][data-tag-owner="{asset_id}"]')


def _chips(page: Page, asset_id: str):
    return _tag_box(page, asset_id).locator(".tag-chip")


@pytest.mark.e2e
class TestAssetTagPicker:
    def test_add_tag_from_row_picker(self, page: Page, api, e2e_server):
        asset = api.create_asset(filename="e2e-tag-add.mp4").json()
        tag = _make_tag(api, "Lobby Loop")

        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")

        chips = _chips(page, asset["id"])
        expect(chips).to_have_count(0)

        _tag_box(page, asset["id"]).locator(".tag-add-btn").click()
        popup = page.locator("#tag-picker-popup")
        expect(popup).to_be_visible(timeout=3000)
        popup.get_by_role("button", name=tag["name"]).click()

        expect(chips).to_have_count(1, timeout=5000)
        expect(chips.first).to_contain_text(tag["name"])

    def test_chip_remove_button_untags_asset(self, page: Page, api, e2e_server):
        asset = api.create_asset(filename="e2e-tag-remove.mp4").json()
        tag_id = _make_tag(api, "Removable")["id"]
        api.post("/api/assets/bulk", json={
            "asset_ids": [asset["id"]], "action": "add_tag", "tag_id": tag_id,
        })

        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")

        chips = _chips(page, asset["id"])
        expect(chips).to_have_count(1)
        chips.first.locator(".tag-chip-remove").click()

        expect(chips).to_have_count(0, timeout=5000)

    def test_picker_reports_when_every_tag_is_applied(
        self, page: Page, api, e2e_server
    ):
        """The picker must say something rather than opening empty."""
        asset = api.create_asset(filename="e2e-tag-exhausted.mp4").json()
        _make_tag(api, "Only Tag")
        # Tags are global and the e2e database is shared across this module, so
        # exhaust whatever exists rather than assuming a single tag.
        for tag in api.get("/api/tags").json():
            api.post("/api/assets/bulk", json={
                "asset_ids": [asset["id"]], "action": "add_tag", "tag_id": tag["id"],
            })

        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")

        _tag_box(page, asset["id"]).locator(".tag-add-btn").click()
        popup = page.locator("#tag-picker-popup")
        expect(popup).to_be_visible(timeout=3000)
        expect(popup).to_contain_text("All tags already applied")
