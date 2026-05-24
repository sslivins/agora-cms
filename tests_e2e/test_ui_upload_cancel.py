"""E2E: cancelling the file picker after a drag-drop should reset the label.

Also covers the post-successful-upload reset (issue #582): after a 201
response the drop-label must revert to the default and the Upload
button must be disabled, so the form looks fresh for the next upload.
"""

import pytest


DEFAULT_LABEL = "Drag & drop files here, or"


def test_cancel_file_picker_resets_label_after_drop(page: "Page"):
    """After drag-dropping a file, clicking Choose File then cancelling
    should clear the displayed filename back to the default label."""

    page.goto("/assets")

    # Simulate a file being set on the hidden input (equivalent to drag-drop)
    file_input = page.locator("#file-input")
    file_input.set_input_files(
        {"name": "test-video.mp4", "mimeType": "video/mp4", "buffer": b"\x00" * 10}
    )

    # Label should now show the filename
    label = page.locator("#drop-label")
    assert label.text_content() == "test-video.mp4"

    # Simulate cancelling the file picker — clears the input
    file_input.set_input_files([])

    # Label should revert to default
    assert label.text_content() == DEFAULT_LABEL


def test_cancel_file_picker_resets_label_after_choose(page: "Page"):
    """After choosing a file via the button, cancelling the picker
    should clear the displayed filename back to the default label."""

    page.goto("/assets")

    file_input = page.locator("#file-input")
    file_input.set_input_files(
        {"name": "photo.jpg", "mimeType": "image/jpeg", "buffer": b"\xFF\xD8" * 5}
    )

    label = page.locator("#drop-label")
    assert label.text_content() == "photo.jpg"

    # Cancel
    file_input.set_input_files([])
    assert label.text_content() == DEFAULT_LABEL


def test_form_resets_after_successful_upload(page: "Page"):
    """Issue #582: after a 201 from /api/assets/upload, the drop-label
    must revert to the default text and the Upload button must be
    disabled — otherwise the form looks like it's still holding the
    file the user just uploaded."""

    page.goto("/assets")

    label = page.locator("#drop-label")
    submit = page.locator("#upload-submit")

    # Sanity: initial state is the default label + disabled button.
    assert label.text_content() == DEFAULT_LABEL
    assert submit.is_disabled()

    file_input = page.locator("#file-input")
    file_input.set_input_files(
        {"name": "reset-me.jpg", "mimeType": "image/jpeg", "buffer": b"\xFF\xD8" * 8}
    )

    # Pre-upload: label shows filename, button enabled.
    assert label.text_content() == "reset-me.jpg"
    assert submit.is_enabled()

    # Fire the upload and wait for the 201.
    with page.expect_response(
        lambda r: "/api/assets/upload" in r.url and r.request.method == "POST",
        timeout=30_000,
    ) as rinfo:
        submit.click()
    resp = rinfo.value
    assert resp.status == 201, f"upload failed: HTTP {resp.status} — {resp.text()[:300]}"

    # Post-upload: label is reset, submit re-disabled.
    # Use expect for both so we wait out the async XHR-load handler.
    from playwright.sync_api import expect
    expect(label).to_have_text(DEFAULT_LABEL, timeout=5_000)
    expect(submit).to_be_disabled(timeout=5_000)

