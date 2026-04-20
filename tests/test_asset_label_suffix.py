"""Unit tests for the ``asset_label_suffix`` Jinja filter.

After asset-icons landed, the type emoji moved to a *prefix* (via
``asset_icon``) and this filter now only carries the trailing
``(mm:ss)`` duration for video / saved-stream assets. These tests
lock that behaviour in.
"""

from types import SimpleNamespace

from cms.models.asset import AssetType
from cms.ui import asset_label_suffix


def _asset(asset_type, duration_seconds=None):
    return SimpleNamespace(
        asset_type=asset_type,
        duration_seconds=duration_seconds,
    )


def test_video_with_duration_shows_mmss_only():
    assert asset_label_suffix(_asset(AssetType.VIDEO, 332)) == " (5:32)"


def test_saved_stream_with_duration_shows_mmss_only():
    # Saved streams must surface their duration too (this was the
    # original #316 bug — the template only did it for VIDEO).
    assert asset_label_suffix(_asset(AssetType.SAVED_STREAM, 125)) == " (2:05)"


def test_saved_stream_without_duration_empty():
    # Capture still in progress — don't render "(None)".
    # The icon is rendered separately as a prefix by `asset_icon`.
    assert asset_label_suffix(_asset(AssetType.SAVED_STREAM, None)) == ""


def test_webpage_has_no_suffix():
    # Icon is a prefix now; no duration for webpages → empty suffix.
    assert asset_label_suffix(_asset(AssetType.WEBPAGE)) == ""


def test_live_stream_has_no_suffix():
    assert asset_label_suffix(_asset(AssetType.STREAM)) == ""


def test_image_has_no_suffix():
    assert asset_label_suffix(_asset(AssetType.IMAGE)) == ""


def test_duration_pads_seconds():
    assert asset_label_suffix(_asset(AssetType.VIDEO, 65)) == " (1:05)"


def test_duration_zero_falsy_empty():
    # A zero-duration asset is effectively unknown — don't render "(0:00)".
    assert asset_label_suffix(_asset(AssetType.VIDEO, 0)) == ""


def test_accepts_string_type_value():
    # Filter is also used on dict-like objects in some paths; tolerate
    # a raw string type (as emitted via `asset_type.value`).
    asset = SimpleNamespace(asset_type="webpage", duration_seconds=None)
    assert asset_label_suffix(asset) == ""
