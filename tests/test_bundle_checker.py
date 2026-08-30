"""Tests for cms.services.bundle_checker.

Mirrors the structure of the deleted tests/test_version_checker.py, but
exercises the new agora-os GitHub-Releases-driven module that replaces
the agora deb-polling version_checker as part of the CMS upgrade-path
migration (M2).  Issue #578 moved the cached bundle out of a module
global and into a shared single-row ``agora_os_latest_bundle`` table,
so the seeding pattern is now ``await bundle_checker.set_latest_bundle(
db_session, stub); await db_session.commit()`` instead of
``bundle_checker._latest_bundle = stub``.
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

        # Trailing (1, 0) marks "released" tier so it sorts above any rc-N pre-release.
        assert _parse_version("1.2.3") == (1, 2, 3, 1, 0)

    def test_strips_v_prefix(self):
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("v1.2.3") == (1, 2, 3, 1, 0)

    def test_test_suffix_equals_unsuffixed(self):
        """``0.0.17-test`` and ``0.0.17`` should compare equal -- we treat the
        ``-test`` label as the same release."""
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("0.0.17-test") == _parse_version("0.0.17")
        assert _parse_version("0.0.17-test") == (0, 0, 17, 1, 0)

    def test_ordering(self):
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("0.0.16-test") < _parse_version("0.0.17-test")
        assert _parse_version("1.0.0") > _parse_version("0.99.99")

    def test_rc_suffix_orders_pre_release(self):
        """#625: ``1.0.0-rc1`` and ``1.0.0-rc2`` must compare distinct so
        a device on rc1 sees rc2 as an available update.  Bare ``1.0.0``
        outranks any rc (matches semver pre-release semantics)."""
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("1.0.0-rc1") < _parse_version("1.0.0-rc2")
        assert _parse_version("1.0.0-rc2") < _parse_version("1.0.0")
        assert _parse_version("1.0.0-rc9") < _parse_version("1.0.0-rc10")

    def test_garbage_returns_empty_tuple(self):
        """Empty tuple compares less than any populated tuple -- same
        fall-back behaviour as the old version_checker._parse_version."""
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("") == ()
        assert _parse_version("not-a-version") == ()

    def test_empty_less_than_anything(self):
        from cms.services.bundle_checker import _parse_version

        assert _parse_version("") < _parse_version("0.0.1")


class TestIsOsUpdateAvailable:
    """``latest`` is required positionally after #578 -- no implicit
    fallback to a module-level cache (the fallback WAS the per-replica
    drift bug).  Every test passes ``latest`` directly."""

    def test_returns_false_when_latest_is_none(self):
        from cms.services import bundle_checker

        assert bundle_checker.is_os_update_available("1.0.0", None) is False

    def test_returns_false_when_current_is_none(self):
        from cms.services import bundle_checker

        assert bundle_checker.is_os_update_available(None, "1.2.3") is False

    def test_returns_true_when_older(self):
        from cms.services import bundle_checker

        assert bundle_checker.is_os_update_available("1.2.2", "1.2.3") is True

    def test_returns_false_when_equal(self):
        from cms.services import bundle_checker

        assert bundle_checker.is_os_update_available("1.2.3", "1.2.3") is False

    def test_returns_false_when_newer(self):
        from cms.services import bundle_checker

        assert bundle_checker.is_os_update_available("1.3.0", "1.2.3") is False

    def test_test_suffix_does_not_count_as_update(self):
        """``0.0.17-test`` and ``0.0.17`` compare equal, so the badge must
        stay dark when the latest matches the device label-stripped."""
        from cms.services import bundle_checker

        assert bundle_checker.is_os_update_available("0.0.17", "0.0.17-test") is False
        assert bundle_checker.is_os_update_available("0.0.17-test", "0.0.17-test") is False

    def test_rc_suffix_reports_update(self):
        """#625: device on ``1.0.0-rc1`` must see ``1.0.0-rc2`` as an
        available update.  Pre-fix the parser dropped the ``rcN`` chunk
        and both versions compared equal, so the badge stayed dark."""
        from cms.services import bundle_checker

        assert bundle_checker.is_os_update_available("1.0.0-rc1", "1.0.0-rc2") is True
        assert bundle_checker.is_os_update_available("1.0.0-rc2", "1.0.0-rc1") is False
        assert bundle_checker.is_os_update_available("1.0.0-rc2", "1.0.0-rc2") is False
        # rc N -> released N is still an update.
        assert bundle_checker.is_os_update_available("1.0.0-rc2", "1.0.0") is True
        # Released -> rc of same N is NOT an update (the rc is older).
        assert bundle_checker.is_os_update_available("1.0.0", "1.0.0-rc2") is False


@pytest.mark.asyncio
class TestCheckNow:
    async def test_check_now_persists_to_shared_row_on_success(self, db_session):
        from cms.services import bundle_checker
        from cms.models.agora_os_channel_bundle import CHANNEL_STABLE

        stub = _stub_bundle("1.2.3")
        with patch.object(
            bundle_checker,
            "_fetch_channel_bundles",
            new_callable=AsyncMock,
            return_value={CHANNEL_STABLE: stub},
        ):
            result = await bundle_checker.check_now(db_session)
        assert result is not None
        assert result.target_version == "1.2.3"
        # And the row is visible to a follow-on read on the same session.
        assert (await bundle_checker.get_latest_os_version(db_session)) == "1.2.3"

    async def test_check_now_keeps_prior_row_on_fetch_failure(self, db_session):
        """If GitHub is flaky, callers should still see the last known good
        value via get_latest_bundle() / get_latest_os_version()."""
        from cms.services import bundle_checker

        prior = _stub_bundle("0.5.0")
        await bundle_checker.set_latest_bundle(db_session, prior)
        await db_session.commit()

        with patch.object(
            bundle_checker,
            "_fetch_channel_bundles",
            new_callable=AsyncMock,
            return_value={},
        ):
            result = await bundle_checker.check_now(db_session)
        # check_now returns the persisted value, not None.
        assert result is not None
        assert result.target_version == "0.5.0"
        assert (await bundle_checker.get_latest_os_version(db_session)) == "0.5.0"


@pytest.mark.asyncio
class TestGetLatestOsVersion:
    async def test_returns_none_when_row_absent(self, db_session):
        from cms.services import bundle_checker

        assert (await bundle_checker.get_latest_os_version(db_session)) is None

    async def test_returns_target_version(self, db_session):
        from cms.services import bundle_checker

        await bundle_checker.set_latest_bundle(db_session, _stub_bundle("0.0.17-test"))
        await db_session.commit()
        assert (await bundle_checker.get_latest_os_version(db_session)) == "0.0.17-test"


@pytest.mark.asyncio
class TestSetLatestBundleUpsert:
    """The per-channel row is unique on ``channel`` (PK).  A second
    ``set_latest_bundle`` for the same channel must overwrite the row in
    place, not insert a duplicate."""

    async def test_upsert_overwrites_existing_row(self, db_session):
        from cms.services import bundle_checker
        from cms.models.agora_os_channel_bundle import (
            AgoraOsChannelBundle,
            CHANNEL_STABLE,
        )
        from sqlalchemy import func, select

        await bundle_checker.set_latest_bundle(db_session, _stub_bundle("1.0.0"))
        await db_session.commit()
        await bundle_checker.set_latest_bundle(db_session, _stub_bundle("1.0.1"))
        await db_session.commit()

        count = await db_session.scalar(
            select(func.count())
            .select_from(AgoraOsChannelBundle)
            .where(AgoraOsChannelBundle.channel == CHANNEL_STABLE)
        )
        assert count == 1
        assert (await bundle_checker.get_latest_os_version(db_session)) == "1.0.1"

    async def test_upsert_stamps_last_success_at(self, db_session):
        from cms.services import bundle_checker

        await bundle_checker.set_latest_bundle(db_session, _stub_bundle("1.0.0"))
        await db_session.commit()
        status = await bundle_checker.get_status(db_session)
        assert status["last_success_at"] is not None


@pytest.mark.asyncio
class TestSharedStateAcrossSessions:
    """Issue #578 invariant: a write through session A must be visible
    to a read through a separate session B (same engine, different
    SQLAlchemy session).  This is the multi-replica correctness
    guarantee that justifies the migration off the module global --
    every CMS replica has its own session pool, but they all share the
    same Postgres row."""

    async def test_write_visible_to_independent_session(self, db_engine):
        from sqlalchemy.ext.asyncio import async_sessionmaker
        from cms.services import bundle_checker

        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        # "Replica A" writes.
        async with factory() as session_a:
            await bundle_checker.set_latest_bundle(session_a, _stub_bundle("3.4.5"))
            await session_a.commit()

        # "Replica B" reads through a completely separate session.
        async with factory() as session_b:
            assert (await bundle_checker.get_latest_os_version(session_b)) == "3.4.5"
            bundle = await bundle_checker.get_latest_bundle(session_b)
            assert bundle is not None
            assert bundle.target_version == "3.4.5"
            assert bundle.release_id == "stub-id"


@pytest.mark.asyncio
class TestFetchLatestBundleFollowsRedirects:
    """Regression tests for the GitHub-asset 302 redirect bug.

    GitHub's release-asset ``browser_download_url`` endpoints always
    302 to a one-time-signed URL on ``objects.githubusercontent.com``.
    httpx defaults to **not** following redirects (unlike ``requests``),
    so without ``follow_redirects=True`` every meta.json fetch silently
    failed and the UI's "Check for updates" toast read
    ``"Latest version: unknown"`` with no diagnostic.
    """

    async def test_meta_json_302_is_followed(self):
        import httpx
        from cms.services import bundle_checker

        releases_url = bundle_checker.AGORA_OS_RELEASES_URL
        meta_redirect_url = "https://github.com/sslivins/agora-os/releases/download/v0.0.21-test/meta.json"
        meta_blob_url = "https://objects.githubusercontent.com/blob/meta.json?token=stub"

        meta_body = {
            "version": "0.0.21-test",
            "min_from_version": "0.0.0",
            "sha256": "deadbeef",
            "size_bytes": 12345,
        }
        release_body = [
            {
                "id": 999,
                "tag_name": "v0.0.21-test",
                "draft": False,
                "prerelease": True,
                "published_at": "2026-05-15T12:00:00Z",
                "assets": [
                    {
                        "name": "agora-bundle-0.0.21-test.tar.zst",
                        "browser_download_url": "https://github.com/sslivins/agora-os/releases/download/v0.0.21-test/agora-bundle-0.0.21-test.tar.zst",
                        "size": 12345,
                    },
                    {
                        "name": "agora-bundle-0.0.21-test.tar.zst.minisig",
                        "browser_download_url": "https://github.com/sslivins/agora-os/releases/download/v0.0.21-test/agora-bundle-0.0.21-test.tar.zst.minisig",
                        "size": 100,
                    },
                    {
                        "name": "agora-bundle-0.0.21-test.tar.zst.sha256",
                        "browser_download_url": "https://github.com/sslivins/agora-os/releases/download/v0.0.21-test/agora-bundle-0.0.21-test.tar.zst.sha256",
                        "size": 80,
                    },
                    {
                        "name": "agora-bundle-0.0.21-test.meta.json",
                        "browser_download_url": meta_redirect_url,
                        "size": 200,
                    },
                ],
            }
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).startswith(releases_url):
                return httpx.Response(200, json=release_body)
            if str(request.url) == meta_redirect_url:
                # Simulate GitHub's behaviour: 302 to a signed blob URL.
                return httpx.Response(302, headers={"location": meta_blob_url})
            if str(request.url) == meta_blob_url:
                return httpx.Response(200, json=meta_body)
            return httpx.Response(404, text=f"unexpected URL {request.url}")

        transport = httpx.MockTransport(handler)

        # Monkey-patch httpx.AsyncClient so the production code path uses
        # the mock transport, but otherwise runs unmodified -- crucially
        # this exercises the real ``follow_redirects`` argument.
        real_async_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        # Reset module state so we can assert _last_error.
        original_last_error = bundle_checker._last_error
        bundle_checker._last_error = None
        try:
            with patch.object(bundle_checker.httpx, "AsyncClient", side_effect=factory):
                bundles = await bundle_checker._fetch_channel_bundles()
            from cms.models.agora_os_channel_bundle import CHANNEL_PRERELEASE

            # The lone release is flagged prerelease=True, so it resolves
            # to the prerelease channel (stable has no eligible release).
            result = bundles.get(CHANNEL_PRERELEASE)
            assert result is not None, (
                f"expected fetcher to follow the meta.json 302; got None. "
                f"_last_error={bundle_checker._last_error!r}"
            )
            assert result.target_version == "0.0.21-test"
            assert result.min_from_version == "0.0.0"
            assert bundle_checker._last_error is None
        finally:
            bundle_checker._last_error = original_last_error

    async def test_meta_json_non_200_sets_last_error(self):
        """Defensive: when meta.json returns a real non-200 (after any
        redirects), _last_error must be populated so get_status() can
        surface it via the debug endpoint. Previously this path only
        emitted a logger.warning and left _last_error=None, which made
        the failure invisible to operators."""
        import httpx
        from cms.services import bundle_checker

        releases_url = bundle_checker.AGORA_OS_RELEASES_URL
        meta_url = "https://github.com/sslivins/agora-os/releases/download/v0.0.21-test/meta.json"

        release_body = [
            {
                "id": 999,
                "tag_name": "v0.0.21-test",
                "draft": False,
                "prerelease": True,
                "published_at": "2026-05-15T12:00:00Z",
                "assets": [
                    {"name": "agora-bundle-0.0.21-test.tar.zst", "browser_download_url": "https://x/a.tar.zst", "size": 1},
                    {"name": "agora-bundle-0.0.21-test.tar.zst.minisig", "browser_download_url": "https://x/a.minisig", "size": 1},
                    {"name": "agora-bundle-0.0.21-test.meta.json", "browser_download_url": meta_url, "size": 1},
                ],
            }
        ]

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url).startswith(releases_url):
                return httpx.Response(200, json=release_body)
            if str(request.url) == meta_url:
                return httpx.Response(500, text="boom")
            return httpx.Response(404)

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        original_last_error = bundle_checker._last_error
        bundle_checker._last_error = None
        try:
            with patch.object(bundle_checker.httpx, "AsyncClient", side_effect=factory):
                bundles = await bundle_checker._fetch_channel_bundles()
            assert bundles == {}
            assert bundle_checker._last_error is not None
            assert "500" in bundle_checker._last_error
        finally:
            bundle_checker._last_error = original_last_error


def _release_with_assets(version: str, *, prerelease: bool, published_at: str) -> dict:
    """Build a GitHub release object (with the four required assets) whose
    meta.json is served inline (browser_download_url encodes the version)."""
    base = f"https://github.com/sslivins/agora-os/releases/download/v{version}"
    return {
        "id": hash(version) & 0xFFFF,
        "tag_name": f"v{version}",
        "draft": False,
        "prerelease": prerelease,
        "published_at": published_at,
        "assets": [
            {"name": f"agora-bundle-{version}.tar.zst", "browser_download_url": f"{base}/bundle.tar.zst", "size": 10},
            {"name": f"agora-bundle-{version}.tar.zst.minisig", "browser_download_url": f"{base}/bundle.minisig", "size": 1},
            {"name": f"agora-bundle-{version}.tar.zst.sha256", "browser_download_url": f"{base}/bundle.sha256", "size": 1},
            {"name": f"agora-bundle-{version}.meta.json", "browser_download_url": f"{base}/meta.json", "size": 1},
        ],
    }


@pytest.mark.asyncio
class TestFetchChannelBundles:
    """The two-channel resolver: ``stable`` = newest non-prerelease
    release, ``prerelease`` = newest release overall (prereleases
    included)."""

    async def _run(self, releases: list) -> dict:
        import httpx
        from cms.services import bundle_checker

        releases_url = bundle_checker.AGORA_OS_RELEASES_URL

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if url.startswith(releases_url):
                return httpx.Response(200, json=releases)
            if url.endswith("/meta.json"):
                ver = url.rsplit("/", 2)[-2].lstrip("v")
                return httpx.Response(
                    200, json={"version": ver, "min_from_version": "0.0.0"}
                )
            return httpx.Response(404, text=f"unexpected {url}")

        transport = httpx.MockTransport(handler)
        real_async_client = httpx.AsyncClient

        def factory(*args, **kwargs):
            kwargs["transport"] = transport
            return real_async_client(*args, **kwargs)

        with patch.object(bundle_checker.httpx, "AsyncClient", side_effect=factory):
            return await bundle_checker._fetch_channel_bundles()

    async def test_channels_resolve_distinct_releases(self):
        from cms.models.agora_os_channel_bundle import (
            CHANNEL_PRERELEASE,
            CHANNEL_STABLE,
        )

        releases = [
            _release_with_assets("1.1.0-rc1", prerelease=True, published_at="2026-06-02T00:00:00Z"),
            _release_with_assets("1.0.0", prerelease=False, published_at="2026-06-01T00:00:00Z"),
        ]
        bundles = await self._run(releases)
        assert bundles[CHANNEL_PRERELEASE].target_version == "1.1.0-rc1"
        assert bundles[CHANNEL_STABLE].target_version == "1.0.0"

    async def test_no_stable_when_only_prereleases(self):
        from cms.models.agora_os_channel_bundle import (
            CHANNEL_PRERELEASE,
            CHANNEL_STABLE,
        )

        releases = [
            _release_with_assets("2.0.0-rc2", prerelease=True, published_at="2026-06-05T00:00:00Z"),
            _release_with_assets("2.0.0-rc1", prerelease=True, published_at="2026-06-04T00:00:00Z"),
        ]
        bundles = await self._run(releases)
        assert bundles[CHANNEL_PRERELEASE].target_version == "2.0.0-rc2"
        assert CHANNEL_STABLE not in bundles

    async def test_both_channels_same_release_when_newest_is_stable(self):
        from cms.models.agora_os_channel_bundle import (
            CHANNEL_PRERELEASE,
            CHANNEL_STABLE,
        )

        releases = [
            _release_with_assets("3.0.0", prerelease=False, published_at="2026-06-10T00:00:00Z"),
            _release_with_assets("2.9.0", prerelease=False, published_at="2026-06-01T00:00:00Z"),
        ]
        bundles = await self._run(releases)
        assert bundles[CHANNEL_STABLE].target_version == "3.0.0"
        assert bundles[CHANNEL_PRERELEASE].target_version == "3.0.0"


@pytest.mark.asyncio
class TestPerChannelReaders:
    """``get_latest_bundle`` / ``get_latest_os_version`` take a channel
    argument and read the matching row independently."""

    async def test_channels_are_independent(self, db_session):
        from cms.services import bundle_checker
        from cms.models.agora_os_channel_bundle import (
            CHANNEL_PRERELEASE,
            CHANNEL_STABLE,
        )

        await bundle_checker.set_latest_bundle(
            db_session, _stub_bundle("1.0.0"), CHANNEL_STABLE
        )
        await bundle_checker.set_latest_bundle(
            db_session, _stub_bundle("1.1.0-rc1"), CHANNEL_PRERELEASE
        )
        await db_session.commit()

        assert (
            await bundle_checker.get_latest_os_version(db_session, CHANNEL_STABLE)
        ) == "1.0.0"
        assert (
            await bundle_checker.get_latest_os_version(db_session, CHANNEL_PRERELEASE)
        ) == "1.1.0-rc1"
        assert (await bundle_checker.get_latest_os_version(db_session)) == "1.0.0"

    async def test_prerelease_absent_returns_none(self, db_session):
        from cms.services import bundle_checker
        from cms.models.agora_os_channel_bundle import (
            CHANNEL_PRERELEASE,
            CHANNEL_STABLE,
        )

        await bundle_checker.set_latest_bundle(
            db_session, _stub_bundle("1.0.0"), CHANNEL_STABLE
        )
        await db_session.commit()
        assert (
            await bundle_checker.get_latest_os_version(db_session, CHANNEL_PRERELEASE)
        ) is None
