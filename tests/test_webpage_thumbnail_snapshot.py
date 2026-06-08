"""Tests for webpage-asset snapshot thumbnails.

Generalizes the composed-slide snapshot pipeline (PR #726) to webpage
assets: a webpage asset's thumbnail is a static JPEG screenshot of its
live URL rendered in headless Chromium in the worker, stored as a normal
``thumbnail``-purpose ``AssetVariant`` and surfaced via the existing
``thumbnail_url``.

Covers:

* Profile filtering — webpage assets only ever get thumbnail-purpose
  variants (a device profile must never emit a useless ``.mp4``).
* ``enqueue_thumbnail`` / ``enqueue_missing_thumbnails`` aliases cover
  the WEBPAGE type now that the service is generalized.
* Create + URL-edit hooks queue a fresh snapshot render.
* Worker render branch — a webpage thumbnail variant is rendered to a
  JPEG and marked READY (Playwright mocked); a webpage routed to a
  device profile / with no URL fails loudly instead of ffmpeg-ing a
  missing file.
* SSRF guard — ``_host_is_safe`` rejects loopback / private /
  link-local / unspecified hosts and ``render_url_to_png`` refuses
  unsafe hosts and non-http(s) schemes.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device_profile import DeviceProfile
from cms.services.transcoder import (
    _profile_emits_for_asset,
    enqueue_for_new_profile,
    enqueue_missing_thumbnails,
    enqueue_thumbnail,
)


def _thumb_profile(name: str = "thumb") -> DeviceProfile:
    return DeviceProfile(
        name=name,
        video_codec="h264",
        video_profile="main",
        max_width=480,
        max_height=270,
        max_fps=1,
        builtin=True,
        purpose="thumbnail",
    )


def _device_profile(name: str = "dev") -> DeviceProfile:
    return DeviceProfile(
        name=name,
        video_codec="h264",
        video_profile="main",
        max_width=1920,
        max_height=1080,
        max_fps=30,
        builtin=True,
        purpose="device",
    )


def _webpage_asset(url: str = "https://example.com/") -> Asset:
    return Asset(
        id=uuid.uuid4(),
        filename=f"webpage-{uuid.uuid4()}",
        asset_type=AssetType.WEBPAGE,
        size_bytes=0,
        checksum="",
        url=url,
    )


async def _make_webpage(db_session, url: str = "https://example.com/") -> Asset:
    asset = _webpage_asset(url)
    db_session.add(asset)
    await db_session.commit()
    return asset


# ───────────────────────── profile applicability ─────────────────────


class TestProfileEmitsForAsset:
    def test_webpage_only_for_thumbnail_profile(self):
        wp = _webpage_asset()
        assert _profile_emits_for_asset(_thumb_profile(), wp) is True
        assert _profile_emits_for_asset(_device_profile(), wp) is False


@pytest.mark.asyncio
class TestEnqueueForNewProfile:
    async def test_device_profile_skips_webpage(self, db_session):
        asset = await _make_webpage(db_session)
        prof = _device_profile("dev-skip-wp")
        db_session.add(prof)
        await db_session.commit()

        await enqueue_for_new_profile(prof.id, db_session)

        rows = (await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == prof.id,
            )
        )).scalars().all()
        assert rows == [], "device profile must not transcode a webpage asset"

    async def test_thumbnail_profile_creates_jpg_for_webpage(self, db_session):
        asset = await _make_webpage(db_session)
        prof = _thumb_profile("thumb-new-wp")
        db_session.add(prof)
        await db_session.commit()

        new_ids = await enqueue_for_new_profile(prof.id, db_session)
        assert len(new_ids) == 1

        rows = (await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == prof.id,
            )
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].filename.endswith(".jpg")


@pytest.mark.asyncio
class TestEnqueueThumbnail:
    async def test_creates_pending_jpg_under_thumbnail_only(self, db_session):
        asset = await _make_webpage(db_session)
        thumb = _thumb_profile("t1-wp")
        dev = _device_profile("d1-wp")
        db_session.add_all([thumb, dev])
        await db_session.commit()

        new_ids = await enqueue_thumbnail(asset, db_session)
        assert len(new_ids) == 1

        rows = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.source_asset_id == asset.id)
        )).scalars().all()
        assert len(rows) == 1
        v = rows[0]
        assert v.profile_id == thumb.id
        assert v.filename.endswith(".jpg")
        assert v.status == VariantStatus.PENDING

    async def test_noop_for_image(self, db_session):
        img = Asset(
            id=uuid.uuid4(), filename="i.jpg", asset_type=AssetType.IMAGE,
            size_bytes=1, checksum="c",
        )
        db_session.add_all([img, _thumb_profile("t2-wp")])
        await db_session.commit()
        assert await enqueue_thumbnail(img, db_session) == []

    async def test_coalesces_when_pending_exists(self, db_session):
        asset = await _make_webpage(db_session)
        db_session.add(_thumb_profile("t3-wp"))
        await db_session.commit()

        first = await enqueue_thumbnail(asset, db_session)
        second = await enqueue_thumbnail(asset, db_session)
        assert len(first) == 1
        assert second == []


@pytest.mark.asyncio
class TestCreateAndEditHooks:
    async def test_create_webpage_queues_snapshot(self, client, db_session):
        db_session.add(_thumb_profile("t-create-wp"))
        await db_session.commit()

        resp = await client.post(
            "/api/assets/webpage",
            json={"url": "https://example.com/", "name": "Example"},
        )
        assert resp.status_code == 201, resp.text
        asset_id = uuid.UUID(resp.json()["id"])

        rows = (await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset_id,
                AssetVariant.deleted_at.is_(None),
            )
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == VariantStatus.PENDING
        assert rows[0].filename.endswith(".jpg")

    async def test_url_edit_requeues_snapshot(self, client, db_session):
        db_session.add(_thumb_profile("t-edit-wp"))
        asset = await _make_webpage(db_session, "https://example.com/")
        await db_session.commit()

        # First render queued out-of-band; mark it READY so a fresh edit
        # must enqueue a brand-new PENDING render.
        thumb = (await db_session.execute(
            select(DeviceProfile).where(DeviceProfile.name == "t-edit-wp")
        )).scalar_one()
        db_session.add(AssetVariant(
            id=uuid.uuid4(), source_asset_id=asset.id, profile_id=thumb.id,
            filename=f"{uuid.uuid4()}.jpg", status=VariantStatus.READY,
        ))
        await db_session.commit()

        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"url": "https://example.org/new"},
        )
        assert resp.status_code == 200, resp.text

        pending = (await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.status == VariantStatus.PENDING,
                AssetVariant.deleted_at.is_(None),
            )
        )).scalars().all()
        assert len(pending) == 1


@pytest.mark.asyncio
class TestBackfill:
    async def test_backfills_missing_then_idempotent(self, db_session):
        asset = await _make_webpage(db_session)
        db_session.add(_thumb_profile("t-bf-wp"))
        await db_session.commit()

        first = await enqueue_missing_thumbnails(db_session)
        assert first >= 1

        second = await enqueue_missing_thumbnails(db_session)
        assert second == 0

        rows = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.source_asset_id == asset.id)
        )).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
class TestWorkerWebpageBranch:
    async def _seed_variant(self, db_session, profile, *, url="https://example.com/"):
        asset = _webpage_asset(url)
        db_session.add(asset)
        db_session.add(profile)
        await db_session.flush()
        variant = AssetVariant(
            id=uuid.uuid4(),
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.jpg",
            status=VariantStatus.PENDING,
        )
        db_session.add(variant)
        await db_session.commit()
        return asset, variant

    async def test_renders_webpage_thumbnail_ready(
        self, db_session, tmp_path, monkeypatch
    ):
        from worker import transcoder as wt
        from worker import composed_render as wcr

        thumb = _thumb_profile("t-worker-wp")
        asset, variant = await self._seed_variant(db_session, thumb)

        async def _fake_png(url):
            assert url == asset.url
            return b"\x89PNG-fake"

        async def _fake_convert(src, dst, *, max_width, max_height):
            dst.write_bytes(b"jpegbytes")
            return True

        class _Storage:
            async def on_file_stored(self, key):
                return None

        monkeypatch.setattr(wcr, "render_url_to_png", _fake_png)
        monkeypatch.setattr(wt, "convert_image", _fake_convert)
        monkeypatch.setattr(wt, "get_storage", lambda: _Storage())

        await wt._transcode_one(variant, db_session, tmp_path)

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.READY
        assert variant.size_bytes > 0
        assert variant.checksum

    async def test_webpage_on_device_profile_fails(self, db_session, tmp_path):
        from worker import transcoder as wt

        dev = _device_profile("d-worker-wp")
        asset, variant = await self._seed_variant(db_session, dev)

        await wt._transcode_one(variant, db_session, tmp_path)
        await db_session.refresh(variant)
        assert variant.status == VariantStatus.FAILED

    async def test_webpage_without_url_fails(self, db_session, tmp_path):
        from worker import transcoder as wt

        thumb = _thumb_profile("t-worker-nourl")
        asset, variant = await self._seed_variant(db_session, thumb, url="")

        await wt._transcode_one(variant, db_session, tmp_path)
        await db_session.refresh(variant)
        assert variant.status == VariantStatus.FAILED


# ───────────────────────── SSRF guard ────────────────────────────────


class TestHostIsSafe:
    @pytest.mark.parametrize(
        "host",
        [
            "",
            "localhost",
            "foo.local",
            "127.0.0.1",      # loopback
            "10.0.0.1",       # private
            "192.168.1.1",    # private
            "169.254.169.254",  # link-local / cloud metadata
            "0.0.0.0",        # unspecified
            "::1",            # loopback (ipv6)
        ],
    )
    def test_rejects_unsafe(self, host):
        from worker.composed_render import _host_is_safe

        assert _host_is_safe(host) is False

    @pytest.mark.parametrize("host", ["8.8.8.8", "1.1.1.1"])
    def test_accepts_public_literal(self, host):
        from worker.composed_render import _host_is_safe

        assert _host_is_safe(host) is True


@pytest.mark.asyncio
class TestRenderUrlToPngGuards:
    async def test_rejects_non_http_scheme(self):
        from worker.composed_render import WebpageRenderError, render_url_to_png

        with pytest.raises(WebpageRenderError):
            await render_url_to_png("ftp://example.com/x")

    async def test_rejects_loopback_host(self):
        from worker.composed_render import WebpageRenderError, render_url_to_png

        with pytest.raises(WebpageRenderError):
            await render_url_to_png("http://127.0.0.1/")
