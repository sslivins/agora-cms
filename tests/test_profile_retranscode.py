"""Tests for re-transcoding variants when profile settings change."""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select


@pytest.mark.asyncio
class TestProfileUpdateRetranscode:
    """Updating transcoding-relevant profile settings should reset variants."""

    async def test_changing_crf_resets_ready_variants(self, client, db_session):
        """Changing CRF should reset READY variants back to PENDING."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="retrans-crf", video_codec="h264", crf=23)
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="vid1.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY,
            size_bytes=500,
        )
        db_session.add(variant)
        await db_session.commit()

        # Update CRF
        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"crf": 18},
        )
        assert resp.status_code == 200

        # Variant should be reset to PENDING
        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING

    async def test_changing_description_does_not_retranscode(self, client, db_session):
        """Changing description should NOT reset variants."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="retrans-desc", video_codec="h264")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="vid2.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY,
            size_bytes=500,
        )
        db_session.add(variant)
        await db_session.commit()

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"description": "Updated description"},
        )
        assert resp.status_code == 200

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.READY

    async def test_changing_resolution_resets_variants(self, client, db_session):
        """Changing max_width should reset READY variants."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="retrans-res", video_codec="h264", max_width=1920)
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="vid3.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY,
            size_bytes=500,
        )
        db_session.add(variant)
        await db_session.commit()

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"max_width": 1280},
        )
        assert resp.status_code == 200

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING

    async def test_retranscode_response_shows_zero_ready(self, client, db_session):
        """After a transcoding-relevant change, ready_variants should be 0."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="retrans-resp", video_codec="h264", crf=23)
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="vid4.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY,
            size_bytes=500,
        )
        db_session.add(variant)
        await db_session.commit()

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"crf": 18},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_variants"] == 1
        assert data["ready_variants"] == 0


@pytest.mark.asyncio
class TestProfileChangeFlagsActiveJobs:
    """Profile changes that trigger re-transcode must flag any in-flight
    VARIANT_TRANSCODE jobs with cancel_requested=True so the worker
    heartbeat can SIGTERM ffmpeg.  Otherwise the old ffmpeg keeps writing
    the variant blob in parallel with the new job, producing corruption."""

    async def _setup_active_job(self, db_session, profile_name, crf=23):
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile
        from shared.models.job import Job, JobType, JobStatus

        profile = DeviceProfile(name=profile_name, video_codec="h264", crf=crf)
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename=f"{profile_name}.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum=uuid.uuid4().hex[:8],
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4", status=VariantStatus.PROCESSING,
        )
        db_session.add(variant)
        await db_session.flush()

        job = Job(
            type=JobType.VARIANT_TRANSCODE,
            target_id=variant.id,
            status=JobStatus.PROCESSING,
        )
        db_session.add(job)
        await db_session.commit()
        return profile, variant, job

    async def test_update_profile_flags_active_transcode(self, client, db_session):
        """Updating a transcoding-relevant field must flag the active Job."""
        profile, variant, job = await self._setup_active_job(db_session, "upd-flag")

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"crf": 18},
        )
        assert resp.status_code == 200

        await db_session.refresh(job)
        assert job.cancel_requested is True, "Active Job must be flagged"

    async def test_update_profile_non_transcode_field_does_not_flag(self, client, db_session):
        """Description-only changes must NOT flag active jobs."""
        profile, variant, job = await self._setup_active_job(db_session, "upd-noflag")

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"description": "just renaming"},
        )
        assert resp.status_code == 200

        await db_session.refresh(job)
        assert job.cancel_requested is False

    async def test_delete_profile_flags_active_transcode(self, client, db_session):
        """Deleting a profile must flag any in-flight jobs."""
        profile, variant, job = await self._setup_active_job(db_session, "del-flag")
        job_id = job.id

        resp = await client.delete(f"/api/profiles/{profile.id}")
        assert resp.status_code == 200

        from shared.models.job import Job
        db_session.expire_all()
        result = await db_session.execute(select(Job).where(Job.id == job_id))
        j = result.scalar_one_or_none()
        assert j is not None and j.cancel_requested is True
