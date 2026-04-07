"""E2E tests for the profile edit modal."""

import pytest
from playwright.sync_api import Page, expect


@pytest.mark.e2e
class TestProfileEditCodecDisplay:
    """Edit modal should show the video codec (read-only)."""

    def test_edit_modal_shows_video_codec(self, page: Page, api, e2e_server):
        """The edit modal should display the video codec as read-only info."""
        resp = api.post("/api/profiles", json={
            "name": "codec-test",
            "video_codec": "h264",
            "video_profile": "main",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="codec-test")
        row.locator("button", has_text="Edit").click()

        modal = page.locator(".modal-box")
        expect(modal).to_be_visible(timeout=3000)

        # Should show "Video Codec" label in the modal
        expect(modal.locator("text=Video Codec")).to_be_visible(timeout=2000)

        # Should show "H.264" as the codec value
        expect(modal.locator("text=/[Hh]\\.?264/")).to_be_visible(timeout=2000)

    def test_edit_modal_shows_h265_codec(self, page: Page, api, e2e_server):
        """Edit modal should display H.265 codec correctly."""
        resp = api.post("/api/profiles", json={
            "name": "codec-h265",
            "video_codec": "h265",
            "video_profile": "main",
        })
        assert resp.status_code == 201

        page.goto("/profiles")
        page.wait_for_load_state("domcontentloaded")

        row = page.locator("tr", has_text="codec-h265")
        row.locator("button", has_text="Edit").click()

        modal = page.locator(".modal-box")
        expect(modal).to_be_visible(timeout=3000)

        # Should show H.265/HEVC
        expect(modal.locator("text=/[Hh]\\.?265|HEVC/")).to_be_visible(timeout=2000)


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
        row.locator("button", has_text="Edit").click()

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
        row.locator("button", has_text="Edit").click()

        modal = page.locator(".modal-box")
        expect(modal).to_be_visible(timeout=3000)

        # Change only description
        desc_input = modal.locator("#ep-desc")
        desc_input.fill("New description")

        # Click Save — should succeed without a confirmation modal
        modal.locator("#ep-save").click()

        # Page should reload (profile updated) — no confirmation dialog
        page.wait_for_url("**/profiles", timeout=5000)
