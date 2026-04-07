"""Tests for profile pixel_format and color_space validation.

Verifies the API rejects incompatible combinations of codec/profile
with pixel format and color space (Fixes #83).
"""

import pytest


# ── Valid pixel formats per codec/profile ─────────────────────────────

VALID_420_FORMATS = {"auto", "yuv420p", "yuv420p10le"}
VALID_422_FORMATS = VALID_420_FORMATS | {"yuv422p", "yuv422p10le"}
ALL_FORMATS = VALID_422_FORMATS | {"yuv444p", "yuv444p10le"}

# 8-bit-only profiles cannot use 10-bit pixel formats
FORMATS_8BIT_ONLY = {"auto", "yuv420p", "yuv422p", "yuv444p"}
FORMATS_10BIT = {"yuv420p10le", "yuv422p10le", "yuv444p10le"}

# HDR color spaces require at least 10-bit capable profiles
HDR_COLOR_SPACES = {"bt2020-pq", "bt2020-hlg"}
SDR_COLOR_SPACES = {"auto", "bt709", "smpte170m"}


def _base_profile(**overrides):
    defaults = {
        "name": "test-validation",
        "video_codec": "h264",
        "video_profile": "main",
        "pixel_format": "auto",
        "color_space": "auto",
    }
    defaults.update(overrides)
    return defaults


@pytest.mark.asyncio
class TestPixelFormatValidation:
    """API should reject pixel formats incompatible with codec/profile."""

    async def test_h264_main_rejects_yuv422p(self, client):
        """H.264 main only supports 4:2:0 — yuv422p should be rejected."""
        body = _base_profile(name="h264-main-422", pixel_format="yuv422p")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_h264_main_rejects_yuv444p(self, client):
        """H.264 main only supports 4:2:0 — yuv444p should be rejected."""
        body = _base_profile(name="h264-main-444", pixel_format="yuv444p")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_h264_baseline_rejects_yuv422p(self, client):
        body = _base_profile(name="h264-bl-422", video_profile="baseline",
                             pixel_format="yuv422p")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_h264_main_rejects_10bit(self, client):
        """H.264 main is 8-bit only — yuv420p10le should be rejected."""
        body = _base_profile(name="h264-main-10bit", pixel_format="yuv420p10le")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_h264_high10_accepts_yuv420p10le(self, client):
        """H.264 high10 supports 10-bit 4:2:0."""
        body = _base_profile(name="h264-hi10-10bit", video_profile="high10",
                             pixel_format="yuv420p10le")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201

    async def test_h264_high10_rejects_yuv422p(self, client):
        """H.264 high10 is still 4:2:0 only."""
        body = _base_profile(name="h264-hi10-422", video_profile="high10",
                             pixel_format="yuv422p")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_h264_high422_accepts_yuv422p(self, client):
        """H.264 high422 supports 4:2:2."""
        body = _base_profile(name="h264-hi422-422", video_profile="high422",
                             pixel_format="yuv422p")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201

    async def test_h265_main_rejects_yuv422p(self, client):
        body = _base_profile(name="h265-main-422", video_codec="h265",
                             video_profile="main", pixel_format="yuv422p")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_h265_main10_rejects_yuv422p(self, client):
        body = _base_profile(name="h265-m10-422", video_codec="h265",
                             video_profile="main10", pixel_format="yuv422p")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_av1_rejects_yuv422p(self, client):
        body = _base_profile(name="av1-422", video_codec="av1",
                             pixel_format="yuv422p")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_av1_accepts_yuv420p(self, client):
        body = _base_profile(name="av1-420", video_codec="av1",
                             pixel_format="yuv420p")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201

    async def test_h264_main_accepts_auto(self, client):
        """Auto should always be accepted — the transcoder handles it."""
        body = _base_profile(name="h264-main-auto", pixel_format="auto")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201

    async def test_h264_main_accepts_yuv420p(self, client):
        body = _base_profile(name="h264-main-420", pixel_format="yuv420p")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201


@pytest.mark.asyncio
class TestColorSpaceValidation:
    """API should reject HDR color spaces for 8-bit-only codec/profiles."""

    async def test_h264_main_rejects_bt2020_pq(self, client):
        """H.264 main is 8-bit SDR — bt2020-pq (HDR10) is nonsensical."""
        body = _base_profile(name="h264-main-pq", color_space="bt2020-pq")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_h264_main_rejects_bt2020_hlg(self, client):
        body = _base_profile(name="h264-main-hlg", color_space="bt2020-hlg")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_h264_baseline_rejects_hdr(self, client):
        body = _base_profile(name="h264-bl-hdr", video_profile="baseline",
                             color_space="bt2020-pq")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_h264_high10_accepts_bt2020_pq(self, client):
        """H.264 high10 supports 10-bit — HDR color space is allowed."""
        body = _base_profile(name="h264-hi10-pq", video_profile="high10",
                             color_space="bt2020-pq")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201

    async def test_h265_main_rejects_hdr(self, client):
        body = _base_profile(name="h265-main-hdr", video_codec="h265",
                             video_profile="main", color_space="bt2020-pq")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 422

    async def test_h265_main10_accepts_hdr(self, client):
        """H.265 main10 supports 10-bit — HDR is fine."""
        body = _base_profile(name="h265-m10-hdr", video_codec="h265",
                             video_profile="main10", color_space="bt2020-pq")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201

    async def test_h264_main_accepts_bt709(self, client):
        body = _base_profile(name="h264-main-709", color_space="bt709")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201

    async def test_h264_main_accepts_auto(self, client):
        body = _base_profile(name="h264-main-csauto", color_space="auto")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201


@pytest.mark.asyncio
class TestUpdateValidation:
    """PUT /api/profiles/{id} should also validate pixel_format and color_space."""

    async def test_update_rejects_incompatible_pixel_format(self, client):
        """Updating pixel_format to an incompatible value should be rejected."""
        body = _base_profile(name="update-pf-test")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201
        pid = resp.json()["id"]

        resp = await client.put(f"/api/profiles/{pid}", json={
            "pixel_format": "yuv422p",
        })
        assert resp.status_code == 422

    async def test_update_rejects_incompatible_color_space(self, client):
        body = _base_profile(name="update-cs-test")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201
        pid = resp.json()["id"]

        resp = await client.put(f"/api/profiles/{pid}", json={
            "color_space": "bt2020-pq",
        })
        assert resp.status_code == 422

    async def test_update_accepts_compatible_pixel_format(self, client):
        body = _base_profile(name="update-pf-ok")
        resp = await client.post("/api/profiles", json=body)
        assert resp.status_code == 201
        pid = resp.json()["id"]

        resp = await client.put(f"/api/profiles/{pid}", json={
            "pixel_format": "yuv420p",
        })
        assert resp.status_code == 200
