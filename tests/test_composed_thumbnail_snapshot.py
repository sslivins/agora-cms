"""Tests for composed-slide snapshot thumbnails (PR #726).

Covers the snapshot pipeline that replaces the live-iframe grid preview:

* Profile filtering — composed slides only ever get thumbnail-purpose
  variants; a device profile must never enqueue a useless ``.mp4`` for a
  composed slide.
* ``enqueue_composed_thumbnail`` — supersede-style enqueue used by the
  layout save hook.
* Save hook — saving a layout queues a fresh snapshot render.
* Idempotent startup backfill.
* Worker render branch — a composed thumbnail variant is rendered to a
  JPEG and marked READY (Playwright + HTML build mocked); a composed
  slide routed to a device profile fails loudly instead of ffmpeg-ing a
  missing file.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.composed_slide import ComposedSlide
from cms.models.device_profile import DeviceProfile
from cms.composed.schema import Cell, WidgetInstance, empty_layout
from cms.services.transcoder import (
    _profile_emits_for_asset,
    enqueue_composed_thumbnail,
    enqueue_for_new_profile,
    enqueue_missing_composed_thumbnails,
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


def _composed_asset() -> Asset:
    return Asset(
        id=uuid.uuid4(),
        filename=f"composed-{uuid.uuid4()}",
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
    )


async def _make_composed(db_session, *, layout=None, is_draft=True):
    asset = _composed_asset()
    db_session.add(asset)
    await db_session.flush()
    cs = ComposedSlide(
        asset_id=asset.id,
        layout_json=(layout or empty_layout()).model_dump(mode="json"),
        is_draft=is_draft,
    )
    db_session.add(cs)
    await db_session.commit()
    return asset, cs


def _text_layout(text: str = "hi"):
    layout = empty_layout()
    layout.widgets.append(
        WidgetInstance(
            id=uuid.uuid4(),
            type="text",
            cell=Cell(row=1, col=1, rowspan=1, colspan=4),
            config={"text": text, "font_size_px": 64},
            config_version=1,
        )
    )
    return layout


# ───────────────────────── profile applicability ─────────────────────


class TestProfileEmitsForAsset:
    def test_composed_only_for_thumbnail_profile(self):
        composed = _composed_asset()
        assert _profile_emits_for_asset(_thumb_profile(), composed) is True
        assert _profile_emits_for_asset(_device_profile(), composed) is False

    def test_video_and_image_always_emit(self):
        vid = Asset(
            id=uuid.uuid4(), filename="v.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1, checksum="a",
        )
        img = Asset(
            id=uuid.uuid4(), filename="i.jpg", asset_type=AssetType.IMAGE,
            size_bytes=1, checksum="b",
        )
        for prof in (_thumb_profile(), _device_profile()):
            assert _profile_emits_for_asset(prof, vid) is True
            assert _profile_emits_for_asset(prof, img) is True


@pytest.mark.asyncio
class TestEnqueueForNewProfile:
    async def test_device_profile_skips_composed(self, db_session):
        asset, _ = await _make_composed(db_session)
        prof = _device_profile("dev-skip")
        db_session.add(prof)
        await db_session.commit()

        new_ids = await enqueue_for_new_profile(prof.id, db_session)

        rows = (await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == prof.id,
            )
        )).scalars().all()
        assert rows == [], "device profile must not transcode a composed slide"
        assert all(i not in new_ids for i in [r.id for r in rows])

    async def test_thumbnail_profile_creates_jpg_for_composed(self, db_session):
        asset, _ = await _make_composed(db_session)
        prof = _thumb_profile("thumb-new")
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

    async def test_skips_asset_with_multiple_existing_variants(self, db_session):
        """Regression: the latest-READY-wins thumbnail flow legitimately
        leaves >1 non-deleted variant per (asset, profile). The
        "already has a variant?" guard must treat that as existence, not
        uniqueness — ``scalar_one_or_none`` raised ``MultipleResultsFound``
        and crashed ``_seed_profiles`` at startup (dev deploy blocker)."""
        asset, _ = await _make_composed(db_session)
        prof = _thumb_profile("thumb-dupes")
        db_session.add(prof)
        await db_session.flush()
        for _ in range(2):
            db_session.add(AssetVariant(
                id=uuid.uuid4(),
                source_asset_id=asset.id,
                profile_id=prof.id,
                filename=f"{uuid.uuid4()}.jpg",
                status=VariantStatus.PENDING,
            ))
        await db_session.commit()

        # Must not raise MultipleResultsFound; existing variants => skip.
        new_ids = await enqueue_for_new_profile(prof.id, db_session)
        assert new_ids == []

        rows = (await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.profile_id == prof.id,
            )
        )).scalars().all()
        assert len(rows) == 2, "must not add a third variant"


@pytest.mark.asyncio
class TestEnqueueComposedThumbnail:
    async def test_creates_pending_jpg_under_thumbnail_only(self, db_session):
        asset, _ = await _make_composed(db_session)
        thumb = _thumb_profile("t1")
        dev = _device_profile("d1")
        db_session.add_all([thumb, dev])
        await db_session.commit()

        new_ids = await enqueue_composed_thumbnail(asset, db_session)
        assert len(new_ids) == 1

        rows = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.source_asset_id == asset.id)
        )).scalars().all()
        assert len(rows) == 1
        v = rows[0]
        assert v.profile_id == thumb.id
        assert v.filename.endswith(".jpg")
        assert v.status == VariantStatus.PENDING

    async def test_noop_for_non_composed(self, db_session):
        img = Asset(
            id=uuid.uuid4(), filename="i.jpg", asset_type=AssetType.IMAGE,
            size_bytes=1, checksum="c",
        )
        db_session.add_all([img, _thumb_profile("t2")])
        await db_session.commit()
        assert await enqueue_composed_thumbnail(img, db_session) == []

    async def test_coalesces_when_pending_exists(self, db_session):
        """A second save while a render is still PENDING does not pile on a
        duplicate — the queued job will snapshot the latest layout anyway."""
        asset, _ = await _make_composed(db_session)
        db_session.add(_thumb_profile("t3"))
        await db_session.commit()

        first = await enqueue_composed_thumbnail(asset, db_session)
        second = await enqueue_composed_thumbnail(asset, db_session)
        assert len(first) == 1
        assert second == []

        rows = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.source_asset_id == asset.id)
        )).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
class TestSaveHookEnqueues:
    async def test_patch_layout_queues_snapshot(self, client, db_session):
        asset, _ = await _make_composed(db_session)
        db_session.add(_thumb_profile("t-save"))
        await db_session.commit()

        resp = await client.patch(
            f"/composed/{asset.id}/layout",
            json=_text_layout("hello").model_dump(mode="json"),
        )
        assert resp.status_code == 200, resp.text

        rows = (await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.deleted_at.is_(None),
            )
        )).scalars().all()
        assert len(rows) == 1
        assert rows[0].status == VariantStatus.PENDING
        assert rows[0].filename.endswith(".jpg")


@pytest.mark.asyncio
class TestBackfill:
    async def test_backfills_missing_then_idempotent(self, db_session):
        asset, _ = await _make_composed(db_session)
        db_session.add(_thumb_profile("t-bf"))
        await db_session.commit()

        first = await enqueue_missing_composed_thumbnails(db_session)
        assert first == 1

        # Second run is a no-op — the slide now has a PENDING thumbnail.
        second = await enqueue_missing_composed_thumbnails(db_session)
        assert second == 0

        rows = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.source_asset_id == asset.id)
        )).scalars().all()
        assert len(rows) == 1

    async def test_skips_slide_with_ready_thumbnail(self, db_session):
        asset, _ = await _make_composed(db_session)
        thumb = _thumb_profile("t-bf2")
        db_session.add(thumb)
        await db_session.flush()
        db_session.add(AssetVariant(
            id=uuid.uuid4(), source_asset_id=asset.id, profile_id=thumb.id,
            filename=f"{uuid.uuid4()}.jpg", status=VariantStatus.READY,
        ))
        await db_session.commit()

        assert await enqueue_missing_composed_thumbnails(db_session) == 0

    async def test_no_thumbnail_profile_is_noop(self, db_session):
        await _make_composed(db_session)
        # No thumbnail profile seeded.
        assert await enqueue_missing_composed_thumbnails(db_session) == 0

    async def test_failed_thumbnail_is_re_enqueued(self, db_session):
        """A previous FAILED snapshot must not block a fresh render on boot."""
        asset, _ = await _make_composed(db_session)
        thumb = _thumb_profile("t-bf3")
        db_session.add(thumb)
        await db_session.flush()
        db_session.add(AssetVariant(
            id=uuid.uuid4(), source_asset_id=asset.id, profile_id=thumb.id,
            filename=f"{uuid.uuid4()}.jpg", status=VariantStatus.FAILED,
        ))
        await db_session.commit()

        assert await enqueue_missing_composed_thumbnails(db_session) == 1
        pending = (await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset.id,
                AssetVariant.status == VariantStatus.PENDING,
            )
        )).scalars().all()
        assert len(pending) == 1


@pytest.mark.asyncio
class TestWorkerComposedBranch:
    async def _seed_variant(self, db_session, profile):
        asset, _ = await _make_composed(db_session, layout=_text_layout())
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

    async def test_renders_composed_thumbnail_ready(
        self, db_session, tmp_path, monkeypatch
    ):
        from worker import transcoder as wt
        from cms.composed import render as crender
        from worker import composed_render as wcr
        from cms.composed.render import ComposedRender

        thumb = _thumb_profile("t-worker")
        asset, variant = await self._seed_variant(db_session, thumb)

        async def _fake_build(db, settings, asset_id, *, verify_asset=None):
            assert asset_id == asset.id
            return ComposedRender(html_bytes=b"<html></html>", has_weather=False)

        async def _fake_png(html_bytes):
            return b"\x89PNG-fake"

        async def _fake_convert(src, dst, *, max_width, max_height):
            dst.write_bytes(b"jpegbytes")
            return True

        class _Storage:
            async def on_file_stored(self, key):
                return None

        monkeypatch.setattr(crender, "build_composed_html", _fake_build)
        monkeypatch.setattr(wcr, "render_composed_to_png", _fake_png)
        monkeypatch.setattr(wt, "convert_image", _fake_convert)
        monkeypatch.setattr(wt, "get_storage", lambda: _Storage())

        await wt._transcode_one(variant, db_session, tmp_path)

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.READY
        assert variant.size_bytes > 0
        assert variant.checksum

    async def test_composed_on_device_profile_fails(
        self, db_session, tmp_path
    ):
        dev = _device_profile("d-worker")
        asset, variant = await self._seed_variant(db_session, dev)

        await db_session.refresh(variant)
        # Device-purpose composed variant should fail loudly, not crash.
        from worker import transcoder as wt

        await wt._transcode_one(variant, db_session, tmp_path)
        await db_session.refresh(variant)
        assert variant.status == VariantStatus.FAILED
