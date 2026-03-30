"""Tests for version comparison logic."""

from cms.services.version_checker import is_update_available


class TestIsUpdateAvailable:
    def test_older_device_has_update(self):
        assert is_update_available("0.7.3", "0.7.5") is True

    def test_same_version_no_update(self):
        assert is_update_available("0.7.5", "0.7.5") is False

    def test_newer_device_no_update(self):
        assert is_update_available("0.7.5", "0.7.3") is False

    def test_major_version_difference(self):
        assert is_update_available("0.7.5", "1.0.0") is True

    def test_empty_device_version(self):
        assert is_update_available("", "0.7.5") is False

    def test_no_latest_version(self):
        assert is_update_available("0.7.5", None) is False

    def test_two_segment_version(self):
        assert is_update_available("0.7", "0.7.1") is True

    def test_newer_two_segment(self):
        assert is_update_available("0.8", "0.7.5") is False
