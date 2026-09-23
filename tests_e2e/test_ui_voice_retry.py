"""The voice builder must not create a second asset when you retry.

Synthesis runs *after* the asset is persisted, so a synthesis failure
leaves the page sitting on a saved asset with the button re-enabled and
an error on screen. The obvious user response -- fix the script, press
the button again -- used to POST a second time, leaving the failed
attempt behind as a 0-byte duplicate in the library.

The routes are stubbed so the failure is deterministic and no real
speech service is needed; what's under test is purely the page's
decision of POST-vs-PUT on the second submit.
"""

import json

import pytest
from playwright.sync_api import Page, expect

FAKE_ASSET_ID = "11111111-2222-3333-4444-555555555555"

VOICES = {
    "available": True,
    "message": None,
    "voices": [
        {
            "name": "en-US-TestNeural",
            "display_name": "Test",
            "locale": "en-US",
            "gender": "Female",
            "styles": [],
        }
    ],
}


@pytest.mark.e2e
class TestVoiceCreateRetry:
    def _stub(self, page: Page, submits: list, generation_status: str):
        page.route(
            "**/api/voice-announcements/voices*",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(VOICES),
            ),
        )

        def on_submit(route):
            req = route.request
            submits.append((req.method, req.url))
            if req.method == "POST":
                route.fulfill(
                    status=201,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "asset_id": FAKE_ASSET_ID,
                            "generation_status": "pending",
                            "edit_url": f"/assets/{FAKE_ASSET_ID}/voice",
                        }
                    ),
                )
            else:
                route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "asset_id": FAKE_ASSET_ID,
                            "generation_status": "pending",
                            "generation_error": None,
                            "last_generated_at": None,
                        }
                    ),
                )

        # Status polling reports the failure that prompts the retry.
        page.route(
            f"**/api/voice-announcements/{FAKE_ASSET_ID}",
            lambda route: route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(
                    {
                        "asset_id": FAKE_ASSET_ID,
                        "generation_status": generation_status,
                        "generation_error": "Synthesis failed (stubbed).",
                        "last_generated_at": None,
                    }
                ),
            )
            if route.request.method == "GET"
            else on_submit(route),
        )
        page.route(
            "**/api/voice-announcements",
            lambda route: on_submit(route),
        )

    def _fill_and_submit(self, page: Page):
        page.fill("#voice-display-name", "Retry Guard Test")
        page.fill("#voice-script-text", "Testing the retry guard.")
        page.wait_for_function(
            "() => !document.getElementById('voice-save-btn').disabled"
        )
        page.click("#voice-save-btn")

    def test_retry_after_synthesis_failure_updates_instead_of_creating(
        self, page: Page, api, e2e_server
    ):
        submits: list = []
        self._stub(page, submits, generation_status="failed")

        page.goto("/assets/new/voice")
        page.wait_for_load_state("domcontentloaded")

        self._fill_and_submit(page)

        # Synthesis failed; the page re-enables the button and reports it.
        page.wait_for_function(
            "() => !document.getElementById('voice-save-btn').disabled"
        )
        assert len(submits) == 1 and submits[0][0] == "POST"

        # The user does the obvious thing and presses it again.
        page.fill("#voice-script-text", "Testing the retry guard, take two.")
        self._fill_and_submit(page)
        page.wait_for_function("() => window.__submitCount === undefined || true")
        page.wait_for_timeout(1500)

        assert len(submits) == 2, f"expected a second submit, got {submits}"
        method, url = submits[1]
        assert method == "PUT", (
            f"retry must update the asset it already created, not POST again "
            f"(got {method} {url})"
        )
        assert FAKE_ASSET_ID in url

    def test_button_relabels_once_the_asset_exists(
        self, page: Page, api, e2e_server
    ):
        """The label is the user-visible signal that the page is no longer
        going to create anything new."""
        submits: list = []
        self._stub(page, submits, generation_status="failed")

        page.goto("/assets/new/voice")
        page.wait_for_load_state("domcontentloaded")

        save = page.locator("#voice-save-btn")
        expect(save).to_have_text("Create Announcement")

        self._fill_and_submit(page)
        page.wait_for_function(
            "() => !document.getElementById('voice-save-btn').disabled"
        )

        expect(save).to_have_text("Save Changes")
