"""E2E: cancelling the file picker after a drag-drop should reset the label."""

import pytest


DEFAULT_LABEL = "Drag & drop a file here, or"


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
