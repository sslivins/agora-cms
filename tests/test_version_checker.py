"""Tests for version comparison logic."""

from unittest.mock import AsyncMock, patch

import pytest

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


@pytest.mark.asyncio
class TestCheckNow:
    async def test_check_now_updates_cache(self):
        from cms.services import version_checker

        original = version_checker._latest_version
        try:
            version_checker._latest_version = None
            with patch.object(version_checker, "_fetch_latest_version", new_callable=AsyncMock, return_value="1.2.3"):
                result = await version_checker.check_now()
            assert result == "1.2.3"
            assert version_checker._latest_version == "1.2.3"
        finally:
            version_checker._latest_version = original

    async def test_check_now_keeps_cache_on_failure(self):
        from cms.services import version_checker

        original = version_checker._latest_version
        try:
            version_checker._latest_version = "0.5.0"
            with patch.object(version_checker, "_fetch_latest_version", new_callable=AsyncMock, return_value=None):
                result = await version_checker.check_now()
            assert result == "0.5.0"
            assert version_checker._latest_version == "0.5.0"
        finally:
            version_checker._latest_version = original
