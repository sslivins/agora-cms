"""E2E tests for the profile edit modal."""

import pytest
from playwright.sync_api import Page, expect

from tests_e2e.conftest import click_row_action


@pytest.mark.e2e
class TestProfileTableAutoDisplay:
    """Profile table should display 'Auto' (not 'Pass-through') for auto values."""

    def test_auto_pixel_format_shows_auto_in_table(self, page: Page, api, e2e_server):
        """When pixel_format is 'auto', table column should say 'Auto'."""
        resp = api.post("/api/profiles", json={
            "name": "auto-pf-test",
            "video_codec": "h264",
            "pixel_format": "auto",
            "color_space": "bt709",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="auto-pf-test")
        # Pixel Format is the 6th column (0-indexed: 5)
        pf_cell = row.locator("td").nth(5)
        expect(pf_cell).to_have_text("Auto")

    def test_auto_color_space_shows_auto_in_table(self, page: Page, api, e2e_server):
        """When color_space is 'auto', table column should say 'Auto'."""
        resp = api.post("/api/profiles", json={
            "name": "auto-cs-test",
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "color_space": "auto",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="auto-cs-test")
        # Color Space is the 7th column (0-indexed: 6)
        cs_cell = row.locator("td").nth(6)
        expect(cs_cell).to_have_text("Auto")

    def test_both_auto_shows_auto_after_edit(self, page: Page, api, e2e_server):
        """Edit a profile to set both fields to 'auto', table should show 'Auto'."""
        resp = api.post("/api/profiles", json={
            "name": "edit-auto-test",
            "video_codec": "h264",
            "pixel_format": "yuv420p",
            "color_space": "bt709",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="edit-auto-test")
        click_row_action(row, "Edit")

        modal = page.locator(".modal-box")
        expect(modal).to_be_visible(timeout=3000)

        # Change both to Auto
        modal.locator("#ep-pf").select_option("auto")
        modal.locator("#ep-cs").select_option("auto")

        # Click Save — may trigger retranscode confirmation if variants exist
        # from assets uploaded by earlier test files
        modal.locator("#ep-save").click()

        # Handle retranscode confirmation modal if it appears
        confirm = page.locator(".modal-overlay .modal-box", has_text="re-transcode")
        try:
            confirm.wait_for(state="visible", timeout=2000)
            confirm.locator("button", has_text="Confirm").click()
        except Exception:
            pass  # No retranscode confirmation needed

        # Wait for save to complete and page to reload
        expect(page.locator(".modal-overlay")).to_have_count(0, timeout=10000)
        page.wait_for_load_state("domcontentloaded")

        # Verify the table shows "Auto" for both columns
        row = page.locator("tr", has_text="edit-auto-test")
        pf_cell = row.locator("td").nth(5)
        cs_cell = row.locator("td").nth(6)
        expect(pf_cell).to_have_text("Auto")
        expect(cs_cell).to_have_text("Auto")


@pytest.mark.e2e
class TestProfileEditCodecDisplay:
    """Edit modal should show the video codec as an editable dropdown."""

    def test_edit_modal_shows_video_codec(self, page: Page, api, e2e_server):
        """The edit modal should display the video codec as a select dropdown."""
        resp = api.post("/api/profiles", json={
            "name": "codec-test",
            "video_codec": "h264",
            "video_profile": "main",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="codec-test")
        click_row_action(row, "Edit")

        modal = page.locator(".modal-box")
        expect(modal).to_be_visible(timeout=3000)

        # Should show "Video Codec" label in the modal
        expect(modal.locator("label", has_text="Video Codec")).to_be_visible(timeout=2000)

        # Should have a codec dropdown with h264 selected
        codec_select = modal.locator("select#ep-vc")
        expect(codec_select).to_be_visible(timeout=2000)
        assert codec_select.input_value() == "h264"

    def test_edit_modal_shows_h265_codec(self, page: Page, api, e2e_server):
        """Edit modal should display H.265 codec selected in dropdown."""
        resp = api.post("/api/profiles", json={
            "name": "codec-h265",
            "video_codec": "h265",
            "video_profile": "main",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="codec-h265")
        click_row_action(row, "Edit")

        modal = page.locator(".modal-box")
        expect(modal).to_be_visible(timeout=3000)

        # Should have h265 selected in the codec dropdown
        codec_select = modal.locator("select#ep-vc")
        expect(codec_select).to_be_visible(timeout=2000)
        assert codec_select.input_value() == "h265"


@pytest.mark.e2e
class TestProfileEditRetranscodeWarning:
    """Saving profile changes to transcoding fields should show a warning."""

    def test_warning_shown_when_changing_crf(self, page: Page, api, e2e_server):
        """Changing CRF should trigger a confirmation modal before saving."""
        # Create profile + upload an asset so variants exist
        resp = api.post("/api/profiles", json={
            "name": "warn-crf",
            "video_codec": "h264",
            "crf": 23,
        })
        assert resp.status_code == 201
        profile_id = resp.json()["id"]

        # Upload an asset to create a variant for this profile
        api.create_asset(filename="warn-video.mp4")

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="warn-crf")
        click_row_action(row, "Edit")

        modal = page.locator(".modal-box")
        expect(modal).to_be_visible(timeout=3000)

        # Change CRF value
        crf_input = modal.locator("#ep-crf")
        crf_input.fill("18")

        # Click Save
        modal.locator("#ep-save").click()

        # Confirmation modal should appear
        confirm_modal = page.locator(".modal-overlay .modal-box", has_text="re-transcode")
        expect(confirm_modal).to_be_visible(timeout=3000)

    def test_no_warning_when_only_description_changes(self, page: Page, api, e2e_server):
        """Changing only description should NOT show a warning."""
        resp = api.post("/api/profiles", json={
            "name": "warn-desc",
            "video_codec": "h264",
        })
        assert resp.status_code == 201

        # Upload asset so variants exist
        api.create_asset(filename="warn-desc-video.mp4")

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="warn-desc")
        click_row_action(row, "Edit")

        modal = page.locator(".modal-box")
        expect(modal).to_be_visible(timeout=3000)

        # Change only description
        desc_input = modal.locator("#ep-desc")
        desc_input.fill("New description")

        # Click Save — should succeed without a confirmation modal
        modal.locator("#ep-save").click()

        # Page should reload (profile updated) — no confirmation dialog
        page.wait_for_url("**/profiles", timeout=5000)
