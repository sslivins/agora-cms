"""Tests for cancelling in-progress transcodes on profile update.

Reproduces the bug where editing a profile while a transcode is running
leaves the PROCESSING variant uncancelled — it completes with stale settings
and is marked READY, never re-queued.

Fixes #91.
"""

import asyncio
import uuid
from unittest.mock import AsyncMock, patch

import pytest

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device_profile import DeviceProfile


# ── API-level tests: profile update should reset PROCESSING variants ──


class TestProfileUpdateResetsProcessing:
    """PUT /api/profiles/{id} should reset PROCESSING variants to PENDING."""

    @pytest.mark.asyncio
    async def test_processing_variant_reset_to_pending(self, client, db_session):
        """Editing a profile while a variant is PROCESSING must cancel the
        existing job and create a new PENDING variant row (swap semantics)."""
        from sqlalchemy import select
        from shared.models.job import Job, JobType, JobStatus

        # Create profile
        profile = DeviceProfile(name="cancel-test", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        # Create asset + variant in PROCESSING state
        asset = Asset(
            filename="cancel-test.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=1000,
            checksum="abc123",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.PROCESSING,
            progress=42.0,
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
        orig_variant_id = variant.id
        orig_job_id = job.id

        # Update profile with a transcoding-relevant change
        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"crf": 18},
        )
        assert resp.status_code == 200

        db_session.expunge_all()
        # Old variant row is still PROCESSING — worker will SIGTERM ffmpeg
        # when it picks up cancel_requested.  A NEW PENDING variant row has
        # been inserted with a fresh UUID.
        variants = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.profile_id == profile.id)
        )).scalars().all()
        assert len(variants) == 2
        old = next(v for v in variants if v.id == orig_variant_id)
        new = next(v for v in variants if v.id != orig_variant_id)
        assert old.status == VariantStatus.PROCESSING
        assert new.status == VariantStatus.PENDING
        assert new.filename != old.filename

        # The old in-flight job is flagged for cooperative cancellation.
        flagged_job = (await db_session.execute(
            select(Job).where(Job.id == orig_job_id)
        )).scalar_one()
        assert flagged_job.cancel_requested is True

    @pytest.mark.asyncio
    async def test_non_transcode_change_leaves_processing_alone(self, client, db_session):
        """Non-transcoding field changes should not reset PROCESSING variants."""
        profile = DeviceProfile(name="no-reset-test", video_codec="h264", video_profile="main")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="no-reset-test.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=1000,
            checksum="def456",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.PROCESSING,
            progress=50.0,
        )
        db_session.add(variant)
        await db_session.commit()

        # Update only description — not a transcode field
        resp = await client.put(
            f"/api/profiles/{profile.id}",
            json={"description": "updated desc"},
        )
        assert resp.status_code == 200

        # PROCESSING variant should be untouched
        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PROCESSING
        assert variant.progress == 50.0


# ── Unit tests: cancel_profile_transcodes function ──
# In the new architecture, ffmpeg runs in the dedicated worker container.
# The CMS cancel functions are no-ops (always return False) — the worker
# handles its own process lifecycle.


class TestCancelProfileTranscodes:
    """cancel_profile_transcodes() is a no-op in CMS (worker handles ffmpeg)."""

    @pytest.mark.asyncio
    async def test_always_returns_false(self):
        """CMS cancel is a no-op — worker manages its own ffmpeg processes."""
        from cms.services import transcoder

        profile_id = uuid.uuid4()
        cancelled = transcoder.cancel_profile_transcodes(profile_id)
        assert cancelled is False

    @pytest.mark.asyncio
    async def test_no_op_for_different_profile(self):
        """Should return False (no-op) regardless of profile."""
        from cms.services import transcoder

        cancelled = transcoder.cancel_profile_transcodes(uuid.uuid4())
        assert cancelled is False

    @pytest.mark.asyncio
    async def test_no_op_when_no_active_transcode(self):
        """Should return False when no transcode is running."""
        from cms.services import transcoder

        cancelled = transcoder.cancel_profile_transcodes(uuid.uuid4())
        assert cancelled is False


class TestTranscodeOneCancellation:
    """_transcode_one should not mark a cancelled variant as FAILED."""

    @pytest.mark.asyncio
    async def test_cancelled_variant_not_marked_failed(self, db_session, tmp_path):
        """When ffmpeg is killed mid-transcode, variant should stay PENDING (not FAILED)."""
        from worker import transcoder

        profile = DeviceProfile(
            name="cancel-transcode-test",
            video_codec="h264",
            video_profile="main",
            max_width=1920,
            max_height=1080,
            max_fps=30,
            crf=23,
            pixel_format="auto",
            color_space="auto",
            audio_codec="aac",
            audio_bitrate="128k",
        )
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="transcode-cancel-test.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=1000,
            checksum="xyz789",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.PENDING,
        )
        db_session.add(variant)
        await db_session.commit()

        # Create a fake source file
        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        (asset_dir / "transcode-cancel-test.mp4").write_bytes(b"fake video data")

        # Mock subprocess that simulates being killed (returncode -15 = SIGTERM)
        mock_proc = AsyncMock()
        mock_proc.returncode = -15  # SIGTERM
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()

        # The variant will be set to PENDING by the profile update before
        # _transcode_one finishes, so we simulate that by setting the
        # _cancelled_variant_ids set
        transcoder._cancelled_variant_ids.add(variant.id)

        with patch("worker.transcoder.asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch("worker.transcoder._get_duration", return_value=60.0), \
             patch("worker.transcoder.probe_media", return_value={}):
            await transcoder._transcode_one(variant, db_session, asset_dir)

        # Variant should NOT be FAILED — the cancel signal means it will be re-queued
        await db_session.refresh(variant)
        assert variant.status != VariantStatus.FAILED, (
            f"Cancelled variant should not be FAILED, got {variant.status}"
        )
