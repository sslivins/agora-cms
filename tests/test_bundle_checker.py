"""Tests for cms.services.bundle_checker.

Mirrors the structure of the deleted tests/test_version_checker.py, but
exercises the new agora-os GitHub-Releases-driven module that replaces
the agora deb-polling version_checker as part of the CMS upgrade-path
migration (M2).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest


def _stub_bundle(version: str = "1.2.3") -> "object":
    from cms.services import bundle_checker

    return bundle_checker.BundleInfo(
        target_version=version,
        release_id="stub-id",
        min_from_version="0.0.0",
        bundle_url="https://example.com/agora-bundle.tar.zst",
        signature_url="https://example.com/agora-bundle.tar.zst.minisig",
        sha256_url=None,
        size_bytes=0,
        created_at="2026-05-15T00:00:00Z",
    )


class TestParseVersion:
    """_parse_version is the comparable-tuple helper used by is_os_update_available."""

    def test_basic_three_part(self):
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("1.2.3") == (1, 2, 3)

    def test_strips_v_prefix(self):
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("v1.2.3") == (1, 2, 3)

    def test_test_suffix_equals_unsuffixed(self):
        """``0.0.17-test`` and ``0.0.17`` should compare equal — we treat the
        ``-test`` label as the same release."""
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("0.0.17-test") == _parse_version("0.0.17")
        assert _parse_version("0.0.17-test") == (0, 0, 17)

    def test_ordering(self):
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("0.0.16-test") < _parse_version("0.0.17-test")
        assert _parse_version("1.0.0") > _parse_version("0.99.99")

    def test_garbage_returns_empty_tuple(self):
        """Empty tuple compares less than any populated tuple — same
        fall-back behaviour as the old version_checker._parse_version."""
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("") == ()
        assert _parse_version("not-a-version") == ()

    def test_empty_less_than_anything(self):
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("") < _parse_version("0.0.1")


class TestIsOsUpdateAvailable:
    def test_returns_false_when_no_cache(self):
        from cms.services import bundle_checker

        original = bundle_checker._latest_bundle
        bundle_checker._latest_bundle = None
        try:
            assert bundle_checker.is_os_update_available("1.0.0") is False
        finally:
            bundle_checker._latest_bundle = original

    def test_returns_false_when_current_is_none(self):
        from cms.services import bundle_checker

        original = bundle_checker._latest_bundle
        bundle_checker._latest_bundle = _stub_bundle("1.2.3")
        try:
            assert bundle_checker.is_os_update_available(None) is False
        finally:
            bundle_checker._latest_bundle = original

    def test_returns_true_when_older(self):
        from cms.services import bundle_checker

        original = bundle_checker._latest_bundle
        bundle_checker._latest_bundle = _stub_bundle("1.2.3")
        try:
            assert bundle_checker.is_os_update_available("1.2.2") is True
        finally:
            bundle_checker._latest_bundle = original

    def test_returns_false_when_equal(self):
        from cms.services import bundle_checker

        original = bundle_checker._latest_bundle
        bundle_checker._latest_bundle = _stub_bundle("1.2.3")
        try:
            assert bundle_checker.is_os_update_available("1.2.3") is False
        finally:
            bundle_checker._latest_bundle = original

    def test_returns_false_when_newer(self):
        from cms.services import bundle_checker

        original = bundle_checker._latest_bundle
        bundle_checker._latest_bundle = _stub_bundle("1.2.3")
        try:
            assert bundle_checker.is_os_update_available("1.3.0") is False
        finally:
            bundle_checker._latest_bundle = original

    def test_test_suffix_does_not_count_as_update(self):
        """``0.0.17-test`` and ``0.0.17`` compare equal, so the badge must
        stay dark when the cached latest matches the device label-stripped."""
        from cms.services import bundle_checker

        original = bundle_checker._latest_bundle
        bundle_checker._latest_bundle = _stub_bundle("0.0.17-test")
        try:
            assert bundle_checker.is_os_update_available("0.0.17") is False
            assert bundle_checker.is_os_update_available("0.0.17-test") is False
        finally:
            bundle_checker._latest_bundle = original

    def test_explicit_latest_overrides_cache(self):
        from cms.services import bundle_checker

        original = bundle_checker._latest_bundle
        bundle_checker._latest_bundle = _stub_bundle("1.0.0")
        try:
            assert bundle_checker.is_os_update_available("1.0.0", latest="2.0.0") is True
        finally:
            bundle_checker._latest_bundle = original


@pytest.mark.asyncio
class TestCheckNow:
    async def test_check_now_updates_cache_on_success(self):
        from cms.services import bundle_checker

        original = bundle_checker._latest_bundle
        try:
            bundle_checker._latest_bundle = None
            stub = _stub_bundle("1.2.3")
            with patch.object(bundle_checker, "_fetch_latest_bundle", new_callable=AsyncMock, return_value=stub):
                result = await bundle_checker.check_now()
            assert result is stub
            assert bundle_checker._latest_bundle is stub
        finally:
            bundle_checker._latest_bundle = original

    async def test_check_now_keeps_prior_cache_on_fetch_failure(self):
        """If GitHub is flaky, callers should still see the last known
        good value via get_latest_bundle() / get_latest_os_version()."""
        from cms.services import bundle_checker

        original = bundle_checker._latest_bundle
        try:
            prior = _stub_bundle("0.5.0")
            bundle_checker._latest_bundle = prior
            with patch.object(bundle_checker, "_fetch_latest_bundle", new_callable=AsyncMock, return_value=None):
                result = await bundle_checker.check_now()
            # check_now returns the cached value, not None.
            assert result is prior
            assert bundle_checker._latest_bundle is prior
        finally:
            bundle_checker._latest_bundle = original


class TestGetLatestOsVersion:
    def test_returns_none_when_empty(self):
        from cms.services import bundle_checker

        original = bundle_checker._latest_bundle
        bundle_checker._latest_bundle = None
        try:
            assert bundle_checker.get_latest_os_version() is None
        finally:
            bundle_checker._latest_bundle = original

    def test_returns_target_version(self):
        from cms.services import bundle_checker

        original = bundle_checker._latest_bundle
        bundle_checker._latest_bundle = _stub_bundle("0.0.17-test")
        try:
            assert bundle_checker.get_latest_os_version() == "0.0.17-test"
        finally:
            bundle_checker._latest_bundle = original
