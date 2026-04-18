"""Tests for variant cleanup on profile/asset deletion.

Verifies that:
- Deleting a profile removes variant DB records and variant files on disk.
- Deleting an asset removes variant DB records and variant files on disk.
- Active transcodes are cancelled when the parent profile or source asset
  is deleted.
"""

import uuid
from unittest.mock import patch

import pytest
from sqlalchemy import select

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device_profile import DeviceProfile


# ── Profile deletion: variant DB + file cleanup ──


@pytest.mark.asyncio
class TestDeleteProfileCleansVariants:
    """DELETE /api/profiles/{id} should remove all variant records and files."""

    async def test_variant_records_deleted(self, client, db_session):
        """Variant DB rows should be removed when profile is deleted."""
        profile = DeviceProfile(name="del-prof", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(filename="dp-test.mp4", asset_type=AssetType.VIDEO,
                      size_bytes=100, checksum="aaa")
        db_session.add(asset)
        await db_session.flush()

        vid = uuid.uuid4()
        variant = AssetVariant(
            id=vid, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{vid}.mp4", status=VariantStatus.READY,
        )
        db_session.add(variant)
        await db_session.commit()

        resp = await client.delete(f"/api/profiles/{profile.id}")
        assert resp.status_code == 200

        result = await db_session.execute(
            select(AssetVariant).where(AssetVariant.id == vid)
        )
        assert result.scalar_one_or_none() is None

    async def test_variant_files_deleted(self, client, db_session):
        """Variant files on disk should be removed when profile is deleted."""
        profile = DeviceProfile(name="del-prof-files", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(filename="dp-file-test.mp4", asset_type=AssetType.VIDEO,
                      size_bytes=100, checksum="bbb")
        db_session.add(asset)
        await db_session.flush()

        vid = uuid.uuid4()
        variant = AssetVariant(
            id=vid, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{vid}.mp4", status=VariantStatus.READY,
        )
        db_session.add(variant)
        await db_session.commit()

        # Create the variant file on disk
        from cms.auth import get_settings
        settings = get_settings()
        variants_dir = settings.asset_storage_path / "variants"
        variants_dir.mkdir(parents=True, exist_ok=True)
        vfile = variants_dir / variant.filename
        vfile.write_bytes(b"fake-variant-data")
        assert vfile.is_file()

        resp = await client.delete(f"/api/profiles/{profile.id}")
        assert resp.status_code == 200
        assert not vfile.is_file(), "Variant file should be deleted from disk"

    async def test_delete_profile_cancels_active_transcode(self, client, db_session):
        """Deleting a profile should cancel an active transcode for that profile."""
        profile = DeviceProfile(name="del-cancel", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(filename="dc-test.mp4", asset_type=AssetType.VIDEO,
                      size_bytes=100, checksum="ccc")
        db_session.add(asset)
        await db_session.flush()

        vid = uuid.uuid4()
        variant = AssetVariant(
            id=vid, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{vid}.mp4", status=VariantStatus.PROCESSING,
        )
        db_session.add(variant)
        await db_session.commit()

        with patch("cms.services.transcoder.cancel_profile_transcodes") as mock_cancel:
            mock_cancel.return_value = True
            resp = await client.delete(f"/api/profiles/{profile.id}")
            assert resp.status_code == 200
            mock_cancel.assert_called_once_with(profile.id)


# ── Asset deletion: variant DB + file cleanup ──


@pytest.mark.asyncio
class TestDeleteAssetCleansVariants:
    """DELETE /api/assets/{id} should remove all variant records and files."""

    async def _create_asset(self, db_session, filename, storage_path):
        """Create an asset directly in the DB and write a fake file on disk."""
        asset = Asset(filename=filename, asset_type=AssetType.VIDEO,
                      size_bytes=100, checksum=uuid.uuid4().hex[:8])
        db_session.add(asset)
        await db_session.flush()
        # Write a dummy source file so the delete endpoint can unlink it
        fpath = storage_path / filename
        fpath.write_bytes(b"fake-source")
        return asset

    async def test_variant_records_deleted(self, client, db_session, app):
        """After reaper runs, variant DB rows should be removed."""
        from cms.auth import get_settings
        from cms.services.transcoder import reap_deleted_assets_once
        settings = app.dependency_overrides[get_settings]()

        profile = DeviceProfile(name="da-prof", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        asset = await self._create_asset(db_session, "da-test.mp4", settings.asset_storage_path)

        vid = uuid.uuid4()
        variant = AssetVariant(
            id=vid, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{vid}.mp4", status=VariantStatus.READY,
        )
        db_session.add(variant)
        await db_session.commit()

        resp = await client.delete(f"/api/assets/{asset.id}")
        assert resp.status_code == 200

        # Soft delete: row still exists but marked deleted.  Reaper tick
        # finalizes removal because there are no active Jobs.
        await reap_deleted_assets_once(db_session)

        result = await db_session.execute(
            select(AssetVariant).where(AssetVariant.id == vid)
        )
        assert result.scalar_one_or_none() is None

    async def test_variant_files_deleted(self, client, db_session, app):
        """After reaper runs, variant files on disk should be removed."""
        from cms.auth import get_settings
        from cms.services.transcoder import reap_deleted_assets_once
        settings = app.dependency_overrides[get_settings]()

        profile = DeviceProfile(name="da-prof-files", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        asset = await self._create_asset(db_session, "da-file-test.mp4", settings.asset_storage_path)

        vid = uuid.uuid4()
        variant = AssetVariant(
            id=vid, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{vid}.mp4", status=VariantStatus.READY,
        )
        db_session.add(variant)
        await db_session.commit()

        variants_dir = settings.asset_storage_path / "variants"
        variants_dir.mkdir(parents=True, exist_ok=True)
        vfile = variants_dir / variant.filename
        vfile.write_bytes(b"fake-variant-data")
        assert vfile.is_file()

        resp = await client.delete(f"/api/assets/{asset.id}")
        assert resp.status_code == 200

        await reap_deleted_assets_once(db_session, settings=settings)
        assert not vfile.is_file(), "Variant file should be deleted from disk after reap"

    async def test_delete_asset_flags_active_transcode(self, client, db_session, app):
        """Deleting an asset should flag active Jobs for cancellation via the DB."""
        from cms.auth import get_settings
        from shared.models.job import Job, JobType, JobStatus
        settings = app.dependency_overrides[get_settings]()

        profile = DeviceProfile(name="da-cancel", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        asset = await self._create_asset(db_session, "da-cancel.mp4", settings.asset_storage_path)

        vid = uuid.uuid4()
        variant = AssetVariant(
            id=vid, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{vid}.mp4", status=VariantStatus.PROCESSING,
        )
        db_session.add(variant)

        job = Job(
            type=JobType.VARIANT_TRANSCODE,
            target_id=vid,
            status=JobStatus.PROCESSING,
        )
        db_session.add(job)
        await db_session.commit()

        resp = await client.delete(f"/api/assets/{asset.id}")
        assert resp.status_code == 200

        await db_session.refresh(asset)
        await db_session.refresh(job)
        assert asset.deleted_at is not None, "Asset should be soft-deleted"
        assert job.cancel_requested is True, "Active Job should be flagged for cancel"
        # Listen-mode parity: delete_asset now also drives the job to a terminal
        # status so the reaper doesn't get stuck waiting for a worker that may
        # never transition it (the worker only transitions in queue mode).
        assert job.status == JobStatus.CANCELLED, "Active Job should be marked CANCELLED"
        assert job.error_message == "Asset deleted by user"

    async def test_delete_asset_lets_reaper_finalize_immediately(self, client, db_session, app):
        """In listen mode, no worker is running to transition Jobs to terminal —
        delete_asset must flip them to CANCELLED itself so the reaper's
        active-job gate falls through and the asset is hard-deleted on the
        very next tick.
        """
        from cms.auth import get_settings
        from cms.services.transcoder import reap_deleted_assets_once
        from shared.models.job import Job, JobType, JobStatus
        settings = app.dependency_overrides[get_settings]()

        profile = DeviceProfile(name="da-listen-finalize", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        asset = await self._create_asset(db_session, "da-listen-finalize.mp4", settings.asset_storage_path)
        vid = uuid.uuid4()
        variant = AssetVariant(
            id=vid, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{vid}.mp4", status=VariantStatus.PROCESSING,
        )
        db_session.add(variant)
        job = Job(type=JobType.VARIANT_TRANSCODE, target_id=vid, status=JobStatus.PENDING)
        db_session.add(job)
        await db_session.commit()

        resp = await client.delete(f"/api/assets/{asset.id}")
        assert resp.status_code == 200

        # One reaper tick should be enough — no active jobs survive delete_asset.
        await reap_deleted_assets_once(db_session, settings=settings)

        # Asset row gone, variant row gone.
        gone = await db_session.execute(select(Asset).where(Asset.id == asset.id))
        assert gone.scalar_one_or_none() is None, "Asset should be hard-deleted on first tick"
        still = await db_session.execute(select(AssetVariant).where(AssetVariant.id == vid))
        assert still.scalar_one_or_none() is None, "Variant should be cascade-deleted"

    async def test_reaper_skips_asset_with_active_job(self, client, db_session, app):
        """Reaper must NOT hard-delete a soft-deleted asset while a Job is still
        active (QUEUED/CLAIMED/PROCESSING). This prevents races with in-flight
        ffmpeg processes writing variant blobs.

        Note: delete_asset now flips active jobs to CANCELLED itself (listen-
        mode fix), so we simulate the queue-mode race directly: soft-delete
        the asset row, leave the Job in PROCESSING (as if the worker is still
        grinding and hasn't observed cancel_requested yet), then run reaper.
        """
        from datetime import datetime, timezone
        from cms.auth import get_settings
        from cms.services.transcoder import reap_deleted_assets_once
        from shared.models.job import Job, JobType, JobStatus
        settings = app.dependency_overrides[get_settings]()

        profile = DeviceProfile(name="reap-skip", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        asset = await self._create_asset(db_session, "reap-skip.mp4", settings.asset_storage_path)
        vid = uuid.uuid4()
        variant = AssetVariant(
            id=vid, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{vid}.mp4", status=VariantStatus.PROCESSING,
        )
        db_session.add(variant)
        job = Job(type=JobType.VARIANT_TRANSCODE, target_id=vid,
                  status=JobStatus.PROCESSING, cancel_requested=True)
        db_session.add(job)
        asset.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()

        await reap_deleted_assets_once(db_session, settings=settings)

        await db_session.refresh(asset)
        assert asset.deleted_at is not None, "Asset remains soft-deleted"
        still = await db_session.execute(select(AssetVariant).where(AssetVariant.id == vid))
        assert still.scalar_one_or_none() is not None, "Variant must survive while job active"

    async def test_reaper_finalizes_after_job_terminates(self, client, db_session, app):
        """Reaper should hard-delete on a later tick once the active Job reaches
        a terminal state (CANCELLED/FAILED/COMPLETED). Same setup as above:
        bypass the HTTP delete handler so the Job stays PROCESSING for the
        first reaper tick."""
        from datetime import datetime, timezone
        from cms.auth import get_settings
        from cms.services.transcoder import reap_deleted_assets_once
        from shared.models.job import Job, JobType, JobStatus
        settings = app.dependency_overrides[get_settings]()

        profile = DeviceProfile(name="reap-finalize", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        asset = await self._create_asset(db_session, "reap-finalize.mp4", settings.asset_storage_path)
        vid = uuid.uuid4()
        variant = AssetVariant(
            id=vid, source_asset_id=asset.id, profile_id=profile.id,
            filename=f"{vid}.mp4", status=VariantStatus.PROCESSING,
        )
        db_session.add(variant)
        job = Job(type=JobType.VARIANT_TRANSCODE, target_id=vid,
                  status=JobStatus.PROCESSING, cancel_requested=True)
        db_session.add(job)
        asset.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()

        # First tick: job still active, reaper skips.
        await reap_deleted_assets_once(db_session, settings=settings)
        await db_session.refresh(asset)
        assert asset.deleted_at is not None

        # Worker finishes cancelling.
        await db_session.refresh(job)
        job.status = JobStatus.CANCELLED
        await db_session.commit()

        # Next tick: reaper completes the delete.
        await reap_deleted_assets_once(db_session, settings=settings)

        gone = await db_session.execute(select(AssetVariant).where(AssetVariant.id == vid))
        assert gone.scalar_one_or_none() is None
        gone_asset = await db_session.execute(select(Asset).where(Asset.id == asset.id))
        assert gone_asset.scalar_one_or_none() is None

    async def test_multiple_variants_all_cleaned(self, client, db_session, app):
        """After reaper runs, all variants across multiple profiles should be deleted."""
        from cms.auth import get_settings
        from cms.services.transcoder import reap_deleted_assets_once
        settings = app.dependency_overrides[get_settings]()

        p1 = DeviceProfile(name="multi-p1", video_codec="h264", video_profile="main")
        p2 = DeviceProfile(name="multi-p2", video_codec="h265", video_profile="main")
        db_session.add_all([p1, p2])
        await db_session.flush()

        asset = await self._create_asset(db_session, "multi-v.mp4", settings.asset_storage_path)

        v1_id, v2_id = uuid.uuid4(), uuid.uuid4()
        v1 = AssetVariant(id=v1_id, source_asset_id=asset.id, profile_id=p1.id,
                          filename=f"{v1_id}.mp4", status=VariantStatus.READY)
        v2 = AssetVariant(id=v2_id, source_asset_id=asset.id, profile_id=p2.id,
                          filename=f"{v2_id}.mp4", status=VariantStatus.PENDING)
        db_session.add_all([v1, v2])
        await db_session.commit()

        resp = await client.delete(f"/api/assets/{asset.id}")
        assert resp.status_code == 200

        await reap_deleted_assets_once(db_session)

        for vid in (v1_id, v2_id):
            result = await db_session.execute(
                select(AssetVariant).where(AssetVariant.id == vid)
            )
            assert result.scalar_one_or_none() is None
