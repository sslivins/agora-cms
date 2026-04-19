"""Unit tests for the ``asset_label_suffix`` Jinja filter.

Ensures saved-stream assets surface their duration in asset dropdowns
the same way videos do, and that webpage/live-stream icons and
empty-suffix cases still render correctly (issue #316).
"""

from types import SimpleNamespace

from cms.models.asset import AssetType
from cms.ui import asset_label_suffix


def _asset(asset_type, duration_seconds=None):
    return SimpleNamespace(
        asset_type=asset_type,
        duration_seconds=duration_seconds,
    )


def test_video_with_duration_shows_mmss():
    assert asset_label_suffix(_asset(AssetType.VIDEO, 332)) == " (5:32)"


def test_saved_stream_with_duration_shows_mmss():
    # The bug this is protecting: saved streams were falling through
    # because the template only rendered duration for VIDEO.
    assert asset_label_suffix(_asset(AssetType.SAVED_STREAM, 125)) == " (2:05)"


def test_saved_stream_without_duration_no_suffix():
    # Capture still in progress — don't render "(None)" or similar.
    assert asset_label_suffix(_asset(AssetType.SAVED_STREAM, None)) == ""


def test_webpage_shows_globe():
    assert asset_label_suffix(_asset(AssetType.WEBPAGE)) == " 🌐"


def test_live_stream_shows_antenna():
    assert asset_label_suffix(_asset(AssetType.STREAM)) == " 📡"


def test_image_no_suffix():
    assert asset_label_suffix(_asset(AssetType.IMAGE)) == ""


def test_duration_pads_seconds():
    assert asset_label_suffix(_asset(AssetType.VIDEO, 65)) == " (1:05)"


def test_duration_zero_falsy_no_suffix():
    # A zero-duration asset is effectively unknown — don't render "(0:00)".
    assert asset_label_suffix(_asset(AssetType.VIDEO, 0)) == ""


def test_accepts_string_type_value():
    # Filter is also used on dict-like objects in some paths; tolerate
    # a raw string type (as emitted via `asset_type.value`).
    asset = SimpleNamespace(asset_type="webpage", duration_seconds=None)
    assert asset_label_suffix(asset) == " 🌐"
