"""Tests for re-transcoding variants when profile settings change.

With the "latest-READY-wins" variant-swap model (see plan.md), a
transcoding-relevant profile edit no longer mutates the existing variant
row; instead a fresh variant row with a new UUID is inserted so that the
old ffmpeg run and the new one write to *different* blob paths, eliminating
the in-place reset race.  The old variant stays READY (so devices keep
serving the last good blob) until the reaper supersession sweep marks it
soft-deleted.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select


@pytest.mark.asyncio
class TestProfileUpdateRetranscode:
    """Updating transcoding-relevant profile settings should create a fresh
    variant row while preserving the old READY variant for device playback."""

    async def test_changing_crf_creates_new_pending_variant(self, client, db_session):
        """Changing CRF must insert a new PENDING variant with a fresh UUID
        while leaving the original READY variant intact (deleted_at IS NULL)."""
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

        orig_variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY,
            size_bytes=500,
        )
        db_session.add(orig_variant)
        await db_session.commit()
        orig_id = orig_variant.id
        orig_filename = orig_variant.filename

        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"crf": 18},
        )
        assert resp.status_code == 200

        # Old variant is still READY and still undeleted — device playback
        # keeps working until the new variant takes over and the reaper
        # supersedes the old one.
        db_session.expunge_all()
        result = await db_session.execute(
            select(AssetVariant).where(AssetVariant.profile_id == profile.id)
        )
        variants = result.scalars().all()
        assert len(variants) == 2, (
            f"expected 2 variants (old READY + new PENDING), got {len(variants)}"
        )

        old = next(v for v in variants if v.id == orig_id)
        new = next(v for v in variants if v.id != orig_id)
        assert old.status == VariantStatus.READY
        assert old.deleted_at is None
        assert old.filename == orig_filename
        assert new.status == VariantStatus.PENDING
        assert new.deleted_at is None
        assert new.filename != orig_filename, "new variant must have fresh blob path"
        assert new.source_asset_id == asset.id

    async def test_changing_description_does_not_create_variant(self, client, db_session):
        """Changing description should NOT create a new variant."""
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

        db_session.expunge_all()
        result = await db_session.execute(
            select(AssetVariant).where(AssetVariant.profile_id == profile.id)
        )
        variants = result.scalars().all()
        assert len(variants) == 1
        assert variants[0].status == VariantStatus.READY

    async def test_changing_resolution_creates_new_pending_variant(self, client, db_session):
        """Changing max_width must insert a new PENDING variant row."""
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

        db_session.expunge_all()
        result = await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.profile_id == profile.id,
                AssetVariant.status == VariantStatus.PENDING,
            )
        )
        pending = result.scalars().all()
        assert len(pending) == 1, "exactly one new PENDING variant expected"

    async def test_retranscode_response_counts_both_old_and_new(self, client, db_session):
        """After a transcoding-relevant change, total_variants includes both
        the preserved READY row and the freshly-created PENDING row."""
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
        # Old READY row is preserved; new PENDING row added → 2 total, 1 ready
        assert data["total_variants"] == 2
        assert data["ready_variants"] == 1

    async def test_double_edit_creates_two_new_variants(self, client, db_session):
        """Two back-to-back PUTs must cancel V_new1 and create V_new2.

        This is the corner case that motivated the swap design: a user
        hits save, realises they want to change another field, hits save
        again.  Both in-flight jobs must be flagged cancel_requested and
        a fresh variant inserted for the most recent PUT.
        """
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile
        from shared.models.job import Job, JobStatus, JobType

        profile = DeviceProfile(name="double-edit", video_codec="h264", crf=23)
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="double-edit.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        orig = AssetVariant(
            source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4", status=VariantStatus.READY,
        )
        db_session.add(orig)
        await db_session.commit()

        resp1 = await client.put(
            f"/api/profiles/{profile.id}",
            json={"crf": 20},
        )
        assert resp1.status_code == 200

        resp2 = await client.put(
            f"/api/profiles/{profile.id}",
            json={"video_bitrate": "5M"},
        )
        assert resp2.status_code == 200

        db_session.expunge_all()
        result = await db_session.execute(
            select(AssetVariant)
            .where(AssetVariant.profile_id == profile.id)
            .order_by(AssetVariant.created_at.asc())
        )
        variants = result.scalars().all()
        assert len(variants) == 3, (
            f"expected 3 variants (original READY + 2 new PENDING), "
            f"got {len(variants)}"
        )
        # Original READY preserved
        assert variants[0].id == orig.id
        assert variants[0].status == VariantStatus.READY
        # Two new PENDING rows, fresh filenames
        assert {v.status for v in variants[1:]} == {VariantStatus.PENDING}
        assert len({v.filename for v in variants}) == 3

        # All jobs targeting variants[1] (V_new1) must be cancel_requested
        # since the second PUT flagged them.  Job for variants[2] (V_new2)
        # remains active.
        jobs_result = await db_session.execute(
            select(Job).where(
                Job.type == JobType.VARIANT_TRANSCODE,
                Job.target_id.in_([variants[1].id, variants[2].id]),
            )
        )
        jobs = jobs_result.scalars().all()
        jobs_by_target = {j.target_id: j for j in jobs}
        assert variants[1].id in jobs_by_target, "V_new1 should have had a job"
        assert jobs_by_target[variants[1].id].cancel_requested is True
        assert variants[2].id in jobs_by_target, "V_new2 should have a job"
        assert jobs_by_target[variants[2].id].cancel_requested is False

    async def test_changing_codec_persists_and_creates_new_variant(self, client, db_session):
        """Changing video_codec must persist to the DB and create a new
        PENDING variant — regression guard for issue #261 where codec was
        silently dropped by the ``ProfileUpdate`` schema."""
        from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="retrans-codec", video_codec="h264", crf=23)
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="vid-codec.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="abc",
        )
        db_session.add(asset)
        await db_session.flush()

        orig = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY,
            size_bytes=500,
        )
        db_session.add(orig)
        await db_session.commit()
        orig_id = orig.id
        profile_id = profile.id

        resp = await client.put(
            f"/api/profiles/{profile_id}",
            json={"video_codec": "hevc"},
        )
        assert resp.status_code == 200, resp.text
        # Response body must reflect the new codec
        assert resp.json()["video_codec"] == "hevc"

        # DB row must reflect the new codec
        db_session.expunge_all()
        refreshed = await db_session.get(DeviceProfile, profile_id)
        assert refreshed.video_codec == "hevc", (
            "video_codec change must persist — it was being dropped by "
            "ProfileUpdate in issue #261"
        )

        # A new PENDING variant must have been created (codec is in
        # _TRANSCODE_FIELDS so supersede fires)
        result = await db_session.execute(
            select(AssetVariant).where(AssetVariant.profile_id == profile_id)
        )
        variants = result.scalars().all()
        assert len(variants) == 2, (
            f"codec change must supersede — expected 2 variants, got {len(variants)}"
        )
        old = next(v for v in variants if v.id == orig_id)
        new = next(v for v in variants if v.id != orig_id)
        assert old.status == VariantStatus.READY
        assert old.deleted_at is None
        assert new.status == VariantStatus.PENDING


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
        db_session.expunge_all()
        result = await db_session.execute(select(Job).where(Job.id == job_id))
        j = result.scalar_one_or_none()
        assert j is not None and j.cancel_requested is True
