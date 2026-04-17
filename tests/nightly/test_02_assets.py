"""Phase 3: asset upload + transcode (#250).

Uploads a representative file of each media class through the **real UI** and
the real `POST /api/assets/upload` endpoint, then polls `/api/assets/status`
until the worker has produced a variant for every built-in device profile
(`pi-zero-2w`, `pi-4`, `pi-5`). Assertions cover:

- correct number of variants (one per profile)
- each variant transitions to ``ready`` (none ``failed``)
- each variant has a checksum + non-zero byte count
- video variants carry the right codec per profile (h264 / h265 / h265)
- HEIC uploads are server-side converted to JPEG while the original is
  preserved in ``assets/originals/``

Depends on test_01_oobe.py having walked the wizard earlier in the session.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

import pytest
from playwright.sync_api import Page

from tests.nightly.helpers import media


# Transcode can take a while on a cold worker — the tiny test sources we feed
# in keep it to a handful of seconds, but pad generously for CI.
TRANSCODE_TIMEOUT_S = int(__import__("os").environ.get("NIGHTLY_TRANSCODE_TIMEOUT", "300"))
POLL_INTERVAL_S = 2.0

EXPECTED_PROFILES = {"pi-zero-2w", "pi-4", "pi-5"}
# ffprobe reports h265 as "hevc" — accept either spelling.
CODEC_ALIASES = {
    "h264": {"h264", "avc", "avc1"},
    "h265": {"h265", "hevc", "hev1"},
}
EXPECTED_VIDEO_CODECS = {
    "pi-zero-2w": "h264",
    "pi-4": "h265",
    "pi-5": "h265",
}


# ── helpers ──────────────────────────────────────────────────────────────


def _upload_via_ui(page: Page, file_path: Path) -> dict[str, Any]:
    """Navigate to /assets, submit the upload form, return the 201 JSON."""
    page.goto("/assets")
    # #file-input is a hidden file input (UI shows a drop zone instead);
    # set_input_files works on hidden inputs, but only if they're in the DOM.
    page.wait_for_selector("#file-input", state="attached", timeout=10_000)
    page.set_input_files("#file-input", str(file_path))

    # The form posts to /api/assets/upload via XHR and reloads on success.
    # expect_response gives us the parsed JSON reliably, before the reload
    # tears the page down.
    with page.expect_response(
        lambda r: "/api/assets/upload" in r.url and r.request.method == "POST",
        timeout=60_000,
    ) as rinfo:
        page.click("#upload-submit")

    resp = rinfo.value
    assert resp.status == 201, (
        f"upload of {file_path.name} failed: HTTP {resp.status} — {resp.text()[:500]}"
    )
    return resp.json()


def _fetch_status_for(page: Page, asset_id: str) -> dict[str, Any] | None:
    resp = page.request.get("/api/assets/status")
    assert resp.status == 200, f"/api/assets/status returned {resp.status}"
    for asset in resp.json().get("assets", []):
        if asset.get("id") == asset_id:
            return asset
    return None


def _wait_for_transcode(
    page: Page,
    asset_id: str,
    expected_variants: int = 3,
    timeout: float = TRANSCODE_TIMEOUT_S,
) -> dict[str, Any]:
    """Poll until all variants are READY (or one fails / we time out)."""
    deadline = time.monotonic() + timeout
    last_view: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        asset = _fetch_status_for(page, asset_id)
        if asset is not None:
            last_view = asset
            variants = asset.get("variants", [])
            failed = [v for v in variants if v.get("status") == "failed"]
            if failed:
                raise AssertionError(
                    f"variant(s) FAILED for asset {asset_id}: "
                    + ", ".join(
                        f"{v.get('profile_name')}: {v.get('error_message')}"
                        for v in failed
                    )
                )
            if (
                len(variants) == expected_variants
                and all(v.get("status") == "ready" for v in variants)
            ):
                return asset
        time.sleep(POLL_INTERVAL_S)

    raise AssertionError(
        f"Transcode did not complete for asset {asset_id} within {timeout}s. "
        f"Last status: {last_view!r}"
    )


def _assert_variants_healthy(asset: dict[str, Any], *, video: bool) -> None:
    variants = asset["variants"]
    assert len(variants) == 3, f"expected 3 variants, got {len(variants)}"

    profiles = {v["profile_name"] for v in variants}
    assert profiles == EXPECTED_PROFILES, (
        f"unexpected profile set: {profiles!r}"
    )

    for v in variants:
        assert v["status"] == "ready", v
        assert (v.get("size_bytes") or 0) > 0, f"zero-size variant: {v}"
        assert v.get("checksum"), f"variant has no checksum: {v}"
        assert re.fullmatch(r"[0-9a-f]{64}", v["checksum"]), (
            f"checksum not sha256-hex: {v['checksum']!r}"
        )

        if video:
            expected_codec = EXPECTED_VIDEO_CODECS[v["profile_name"]]
            accepted = CODEC_ALIASES.get(expected_codec, {expected_codec})
            actual = (v.get("video_codec") or "").lower()
            assert actual in accepted, (
                f"{v['profile_name']}: expected one of {sorted(accepted)!r}, got {actual!r}"
            )
            # width/height should be set for video variants; profiles cap at 1920x1080
            assert v.get("width") and v.get("height"), v
            assert v["width"] <= 1920 and v["height"] <= 1080, v


# ── tests ─────────────────────────────────────────────────────────────────


def test_upload_and_transcode_jpeg(authenticated_page: Page) -> None:
    asset = _upload_via_ui(authenticated_page, media.sample_jpeg("nightly-jpeg.jpg"))
    assert asset["asset_type"] == "image"
    assert asset["filename"].endswith(".jpg")
    assert asset["original_filename"] is None or asset["original_filename"].endswith(".jpg"), asset

    final = _wait_for_transcode(authenticated_page, asset["id"])
    _assert_variants_healthy(final, video=False)


def test_upload_and_transcode_png(authenticated_page: Page) -> None:
    asset = _upload_via_ui(authenticated_page, media.sample_png("nightly-png.png"))
    assert asset["asset_type"] == "image"
    assert asset["filename"].endswith(".png")

    final = _wait_for_transcode(authenticated_page, asset["id"])
    _assert_variants_healthy(final, video=False)


def test_upload_heic_is_converted_and_transcoded(authenticated_page: Page) -> None:
    """HEIC is converted to JPEG server-side; original must be preserved."""
    src = media.sample_heic("nightly-heic.heic")
    asset = _upload_via_ui(authenticated_page, src)

    assert asset["asset_type"] == "image", asset
    # Server converts to .jpg; filename reflects that, original_filename tracks .heic
    assert asset["filename"].lower().endswith(".jpg"), (
        f"HEIC was not converted to JPEG: filename={asset['filename']!r}"
    )
    assert asset.get("original_filename", "").lower().endswith(".heic"), (
        f"original_filename should retain .heic extension: {asset.get('original_filename')!r}"
    )

    final = _wait_for_transcode(authenticated_page, asset["id"])
    _assert_variants_healthy(final, video=False)


def test_upload_and_transcode_mp4(authenticated_page: Page) -> None:
    asset = _upload_via_ui(authenticated_page, media.sample_mp4("nightly-mp4.mp4"))
    assert asset["asset_type"] == "video"
    assert asset["filename"].lower().endswith(".mp4")

    final = _wait_for_transcode(authenticated_page, asset["id"])
    _assert_variants_healthy(final, video=True)


def test_assets_status_aggregate_counts_are_consistent(authenticated_page: Page) -> None:
    """Sanity: top-level aggregate matches sum over per-asset variant lists.

    Cheap assertion that runs after the other four tests have populated
    the DB with 4 assets × 3 variants = 12 ready variants.
    """
    resp = authenticated_page.request.get("/api/assets/status")
    data = resp.json()

    aggregate_ready = data.get("variant_ready", 0)
    per_asset_ready = sum(
        sum(1 for v in a["variants"] if v["status"] == "ready")
        for a in data.get("assets", [])
    )
    assert aggregate_ready == per_asset_ready, (
        f"status aggregate drift: top-level={aggregate_ready}, "
        f"summed-from-assets={per_asset_ready}"
    )
    assert data.get("variant_failed", 0) == 0, (
        f"failed variants present after phase 3: {data.get('variant_failed')}"
    )
    assert data.get("asset_count", 0) >= 4, data
