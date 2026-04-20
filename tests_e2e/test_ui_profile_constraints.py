"""E2E tests for profile form pixel format / color space constraints.

Verifies the UI disables incompatible pixel format and color space
options based on the selected codec/profile (Fixes #83).
"""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import click_row_action


@pytest.mark.e2e
class TestCreateFormPixelFormatConstraints:
    """Create form should disable incompatible pixel format options."""

    def test_h264_main_disables_422_formats(self, page: Page, e2e_server):
        """H.264 main should disable yuv422p and yuv444p options."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        # Default is H.264 main — check that 422/444 are disabled
        pf_select = page.locator('select[name="pixel_format"]')
        expect(pf_select.locator('option[value="yuv422p"]')).to_be_disabled()
        expect(pf_select.locator('option[value="yuv444p"]')).to_be_disabled()
        expect(pf_select.locator('option[value="yuv422p10le"]')).to_be_disabled()
        expect(pf_select.locator('option[value="yuv444p10le"]')).to_be_disabled()

    def test_h264_main_disables_10bit_formats(self, page: Page, e2e_server):
        """H.264 main is 8-bit — 10-bit formats should be disabled."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        pf_select = page.locator('select[name="pixel_format"]')
        expect(pf_select.locator('option[value="yuv420p10le"]')).to_be_disabled()

    def test_h264_main_allows_auto_and_yuv420p(self, page: Page, e2e_server):
        """Auto and yuv420p should remain enabled for H.264 main."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        pf_select = page.locator('select[name="pixel_format"]')
        expect(pf_select.locator('option[value="auto"]')).to_be_enabled()
        expect(pf_select.locator('option[value="yuv420p"]')).to_be_enabled()


@pytest.mark.e2e
class TestCreateFormColorSpaceConstraints:
    """Create form should disable HDR color spaces for 8-bit profiles."""

    def test_h264_main_disables_hdr_color_spaces(self, page: Page, e2e_server):
        """H.264 main is 8-bit SDR — HDR color spaces should be disabled."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        cs_select = page.locator('select[name="color_space"]')
        expect(cs_select.locator('option[value="bt2020-pq"]')).to_be_disabled()
        expect(cs_select.locator('option[value="bt2020-hlg"]')).to_be_disabled()

    def test_h264_main_allows_sdr_color_spaces(self, page: Page, e2e_server):
        """SDR color spaces should remain enabled."""
        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        cs_select = page.locator('select[name="color_space"]')
        expect(cs_select.locator('option[value="auto"]')).to_be_enabled()
        expect(cs_select.locator('option[value="bt709"]')).to_be_enabled()
        expect(cs_select.locator('option[value="smpte170m"]')).to_be_enabled()


@pytest.mark.e2e
class TestEditModalConstraints:
    """Edit modal should also enforce pixel format / color space constraints."""

    def test_edit_modal_disables_422_for_h264_main(self, page: Page, api, e2e_server):
        """Edit modal for an H.264 main profile should disable 4:2:2 formats."""
        resp = api.post("/api/profiles", json={
            "name": "edit-constraint-test",
            "video_codec": "h264",
            "video_profile": "main",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="edit-constraint-test")
        click_row_action(row, "Edit")

        modal = page.locator(".modal-box")
        expect(modal).to_be_visible(timeout=3000)

        pf_select = modal.locator("#ep-pf")
        expect(pf_select.locator('option[value="yuv422p"]')).to_be_disabled()
        expect(pf_select.locator('option[value="yuv444p"]')).to_be_disabled()

    def test_edit_modal_disables_hdr_for_h264_main(self, page: Page, api, e2e_server):
        """Edit modal for H.264 main should disable HDR color spaces."""
        resp = api.post("/api/profiles", json={
            "name": "edit-cs-constraint",
            "video_codec": "h264",
            "video_profile": "main",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="edit-cs-constraint")
        click_row_action(row, "Edit")

        modal = page.locator(".modal-box")
        expect(modal).to_be_visible(timeout=3000)

        cs_select = modal.locator("#ep-cs")
        expect(cs_select.locator('option[value="bt2020-pq"]')).to_be_disabled()
        expect(cs_select.locator('option[value="bt2020-hlg"]')).to_be_disabled()
