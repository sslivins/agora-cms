"""E2E: asset preview modal picks the correct media element.

Regression guard for the saved_stream preview bug: previewAsset /
previewVariant used to branch on ``assetType === "video"`` only, so
saved_stream assets rendered the MP4 into an <img> tag and silently
showed nothing.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from .conftest import click_row_action


def _create_saved_stream(api) -> dict:
    """Create a saved_stream asset via the REST API."""
    resp = api.post(
        "/api/assets/stream",
        json={
            "url": "https://example.com/live/preview-test.m3u8",
            "name": "preview-test-stream.m3u8",
            "save_locally": True,
            "capture_duration": 30,
        },
    )
    assert resp.status_code == 201, f"Stream create failed: {resp.status_code} {resp.text}"
    return resp.json()


def test_saved_stream_preview_uses_video_element(page: Page, api):
    """Clicking Preview on a saved_stream row must render a <video>, not <img>.

    The worker is not running in e2e so the capture never completes and the
    <video> element will 404 trying to load its src — but the JS branching
    (which is what the original bug was in) runs entirely client-side before
    the network fetch, so asserting the element type is sufficient.
    """
    asset = _create_saved_stream(api)
    try:
        assert asset["asset_type"] == "saved_stream"

        page.goto("/assets")
        page.wait_for_load_state("domcontentloaded")

        # Scope to the row for our asset, then click its Preview button
        # (Preview now lives inside the row's kebab menu — #249.)
        row = page.locator(f'tr[data-asset-id="{asset["id"]}"]').first
        click_row_action(row, "Preview")

        # The preview modal must contain a <video> element, NOT an <img>
        modal = page.locator(".modal-overlay").last
        expect(modal.locator("video.preview-media")).to_have_count(1)
        expect(modal.locator("img.preview-media")).to_have_count(0)

        # And the <video> src must point at the asset preview endpoint
        src = modal.locator("video.preview-media").get_attribute("src")
        assert src is not None and src.endswith(f"/api/assets/{asset['id']}/preview"), (
            f"Unexpected video src: {src!r}"
        )
    finally:
        api.delete(f"/api/assets/{asset['id']}")
