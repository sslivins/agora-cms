"""Regression tests for the image-variant supersede filter.

Image variants only depend on ``max_width``/``max_height`` (see
``shared.services.image.convert_image``).  A profile edit that touches
only video-specific fields (codec, bitrate, crf, fps, audio, pixel
format, color space) must NOT create fresh PENDING image variants —
doing so was producing pointless re-encode jobs that burned CPU and
briefly dropped image playback readiness for no observable gain.
"""

import uuid

import pytest
from sqlalchemy import select


@pytest.mark.asyncio
class TestImageSupersedeFilter:
    async def _seed_profile_with_video_and_image(self, db_session):
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(
            name="img-supersede-guard",
            video_codec="h264",
            crf=23,
            max_width=1920,
            max_height=1080,
        )
        db_session.add(profile)
        await db_session.flush()

        video = Asset(
            filename="clip.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="vidchk",
        )
        image = Asset(
            filename="poster.jpg", asset_type=AssetType.IMAGE,
            size_bytes=500, checksum="imgchk",
        )
        db_session.add_all([video, image])
        await db_session.flush()

        vv = AssetVariant(
            source_asset_id=video.id, profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY, size_bytes=800,
        )
        iv = AssetVariant(
            source_asset_id=image.id, profile_id=profile.id,
            filename=f"{uuid.uuid4()}.jpg",
            status=VariantStatus.READY, size_bytes=400,
        )
        db_session.add_all([vv, iv])
        await db_session.commit()
        return profile, video, image, vv, iv

    async def test_codec_change_does_not_supersede_images(self, client, db_session):
        """Changing video_codec re-transcodes the video variant but must
        leave image variants untouched."""
        from cms.models.asset import AssetVariant, AssetType, VariantStatus

        profile, video, image, vv_orig, iv_orig = (
            await self._seed_profile_with_video_and_image(db_session)
        )

        resp = await client.put(
            f"/api/profiles/{profile.id}", json={"video_codec": "hevc"},
        )
        assert resp.status_code == 200, resp.text

        db_session.expunge_all()
        variants = (
            await db_session.execute(
                select(AssetVariant).where(AssetVariant.profile_id == profile.id)
            )
        ).scalars().all()

        video_variants = [v for v in variants if v.source_asset_id == video.id]
        image_variants = [v for v in variants if v.source_asset_id == image.id]

        # Video got a fresh PENDING variant alongside the old READY one
        assert len(video_variants) == 2, (
            f"video should have been superseded, got {len(video_variants)}"
        )
        assert {v.status for v in video_variants} == {
            VariantStatus.READY, VariantStatus.PENDING,
        }

        # Image was left alone — still exactly one READY variant
        assert len(image_variants) == 1, (
            f"image must NOT be superseded on codec-only change, "
            f"got {len(image_variants)}"
        )
        assert image_variants[0].id == iv_orig.id
        assert image_variants[0].status == VariantStatus.READY

    async def test_resolution_change_supersedes_images_too(self, client, db_session):
        """Changing max_width DOES affect image output, so images must be
        re-rendered alongside videos."""
        from cms.models.asset import AssetVariant, VariantStatus

        profile, video, image, *_ = (
            await self._seed_profile_with_video_and_image(db_session)
        )

        resp = await client.put(
            f"/api/profiles/{profile.id}", json={"max_width": 1280},
        )
        assert resp.status_code == 200, resp.text

        db_session.expunge_all()
        variants = (
            await db_session.execute(
                select(AssetVariant).where(AssetVariant.profile_id == profile.id)
            )
        ).scalars().all()
        video_variants = [v for v in variants if v.source_asset_id == video.id]
        image_variants = [v for v in variants if v.source_asset_id == image.id]

        assert len(video_variants) == 2
        assert len(image_variants) == 2, (
            "image must be superseded when max_width changes"
        )
        assert {v.status for v in image_variants} == {
            VariantStatus.READY, VariantStatus.PENDING,
        }

    async def test_bitrate_change_does_not_supersede_images(self, client, db_session):
        """video_bitrate is a video-only knob — images stay put."""
        from cms.models.asset import AssetVariant, VariantStatus

        profile, video, image, *_ = (
            await self._seed_profile_with_video_and_image(db_session)
        )

        resp = await client.put(
            f"/api/profiles/{profile.id}", json={"video_bitrate": "4M"},
        )
        assert resp.status_code == 200, resp.text

        db_session.expunge_all()
        image_variants = (
            await db_session.execute(
                select(AssetVariant).where(
                    AssetVariant.profile_id == profile.id,
                    AssetVariant.source_asset_id == image.id,
                )
            )
        ).scalars().all()
        assert len(image_variants) == 1
        assert image_variants[0].status == VariantStatus.READY


@pytest.mark.asyncio
class TestSupersedeHelper:
    """Direct unit coverage for the supersede helper's changed_fields flag."""

    async def test_no_changed_fields_legacy_behaviour_supersedes_everything(
        self, db_session
    ):
        """When callers don't pass changed_fields (pre-#261 behaviour),
        every live variant — including images — is superseded.  This
        preserves backwards compatibility for any future caller that
        doesn't have a change set handy."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile
        from cms.services.transcoder import supersede_profile_variants

        profile = DeviceProfile(name="legacy-helper", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()

        image = Asset(
            filename="still.jpg", asset_type=AssetType.IMAGE,
            size_bytes=100, checksum="c",
        )
        db_session.add(image)
        await db_session.flush()
        db_session.add(AssetVariant(
            source_asset_id=image.id, profile_id=profile.id,
            filename=f"{uuid.uuid4()}.jpg",
            status=VariantStatus.READY, size_bytes=50,
        ))
        await db_session.commit()

        new_ids = await supersede_profile_variants(db_session, profile.id)
        await db_session.commit()
        assert len(new_ids) == 1, (
            "legacy (no changed_fields) callers must still supersede images"
        )

    async def test_video_only_changed_fields_skip_images(self, db_session):
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile
        from cms.services.transcoder import supersede_profile_variants

        profile = DeviceProfile(name="helper-codec-only", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()
        image = Asset(
            filename="pic.jpg", asset_type=AssetType.IMAGE,
            size_bytes=100, checksum="c2",
        )
        db_session.add(image)
        await db_session.flush()
        db_session.add(AssetVariant(
            source_asset_id=image.id, profile_id=profile.id,
            filename=f"{uuid.uuid4()}.jpg",
            status=VariantStatus.READY, size_bytes=50,
        ))
        await db_session.commit()

        new_ids = await supersede_profile_variants(
            db_session, profile.id, changed_fields={"video_codec", "crf"},
        )
        await db_session.commit()
        assert new_ids == []

    async def test_dimension_change_supersedes_images(self, db_session):
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile
        from cms.services.transcoder import supersede_profile_variants

        profile = DeviceProfile(name="helper-dim", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()
        image = Asset(
            filename="pic2.jpg", asset_type=AssetType.IMAGE,
            size_bytes=100, checksum="c3",
        )
        db_session.add(image)
        await db_session.flush()
        db_session.add(AssetVariant(
            source_asset_id=image.id, profile_id=profile.id,
            filename=f"{uuid.uuid4()}.jpg",
            status=VariantStatus.READY, size_bytes=50,
        ))
        await db_session.commit()

        new_ids = await supersede_profile_variants(
            db_session, profile.id,
            changed_fields={"video_codec", "max_height"},
        )
        await db_session.commit()
        assert len(new_ids) == 1
