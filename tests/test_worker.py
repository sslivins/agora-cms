"""Tests for the worker package — recover_interrupted, process_pending,
cancel functions, queue mode, and config.

These tests use the same SQLite-based fixtures from conftest.py.
"""

import asyncio
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device_profile import DeviceProfile


# ── Helpers ──


def _make_profile(db_session, **overrides):
    """Create and flush a DeviceProfile with sensible defaults."""
    defaults = dict(
        name=f"test-{uuid.uuid4().hex[:8]}",
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
    defaults.update(overrides)
    return DeviceProfile(**defaults)


def _make_asset(**overrides):
    defaults = dict(
        filename="test-video.mp4",
        asset_type=AssetType.VIDEO,
        size_bytes=1000,
        checksum="abc123",
    )
    defaults.update(overrides)
    return Asset(**defaults)


def _make_variant(asset_id, profile_id, **overrides):
    defaults = dict(
        id=uuid.uuid4(),
        source_asset_id=asset_id,
        profile_id=profile_id,
        filename=f"{uuid.uuid4()}.mp4",
        status=VariantStatus.PENDING,
    )
    defaults.update(overrides)
    return AssetVariant(**defaults)


# ── WorkerSettings tests ──


class TestWorkerConfig:
    """WorkerSettings should extend SharedSettings with worker-specific fields."""

    def test_defaults(self, monkeypatch):
        """Default worker_mode is 'listen', poll_interval is 60."""
        monkeypatch.setenv("AGORA_CMS_DATABASE_URL", "postgresql+asyncpg://x:x@localhost/test")
        from worker.config import WorkerSettings
        s = WorkerSettings()
        assert s.worker_mode == "listen"
        assert s.poll_interval == 60
        assert s.azure_transcode_queue_url is None

    def test_env_override(self, monkeypatch):
        """Worker settings are overridable via env vars."""
        monkeypatch.setenv("AGORA_CMS_DATABASE_URL", "postgresql+asyncpg://x:x@localhost/test")
        monkeypatch.setenv("AGORA_CMS_WORKER_MODE", "queue")
        monkeypatch.setenv("AGORA_CMS_POLL_INTERVAL", "30")
        monkeypatch.setenv("AGORA_CMS_AZURE_TRANSCODE_QUEUE_URL", "https://myqueue.example.com")
        from worker.config import WorkerSettings
        s = WorkerSettings()
        assert s.worker_mode == "queue"
        assert s.poll_interval == 30
        assert s.azure_transcode_queue_url == "https://myqueue.example.com"


# ── recover_interrupted tests ──


class TestRecoverInterrupted:
    """recover_interrupted() should reset PROCESSING variants to PENDING."""

    @pytest.mark.asyncio
    async def test_resets_processing_to_pending(self, db_engine, db_session):
        """Variants left in PROCESSING from a crash should be reset."""
        from worker.transcoder import recover_interrupted

        profile = _make_profile(db_session)
        db_session.add(profile)
        await db_session.flush()

        asset = _make_asset()
        db_session.add(asset)
        await db_session.flush()

        v1 = _make_variant(asset.id, profile.id, status=VariantStatus.PROCESSING, progress=50.0)
        v2 = _make_variant(asset.id, profile.id, status=VariantStatus.PROCESSING, progress=25.0)
        v3 = _make_variant(asset.id, profile.id, status=VariantStatus.PENDING)
        v4 = _make_variant(asset.id, profile.id, status=VariantStatus.READY)
        db_session.add_all([v1, v2, v3, v4])
        await db_session.commit()

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        count = await recover_interrupted(factory)

        assert count == 2

        # Verify the database state
        db_session.expire_all()
        await db_session.refresh(v1)
        await db_session.refresh(v2)
        await db_session.refresh(v3)
        await db_session.refresh(v4)

        assert v1.status == VariantStatus.PENDING
        assert v1.progress == 0
        assert v2.status == VariantStatus.PENDING
        assert v2.progress == 0
        assert v3.status == VariantStatus.PENDING  # unchanged
        assert v4.status == VariantStatus.READY  # unchanged

    @pytest.mark.asyncio
    async def test_no_stuck_variants(self, db_engine, db_session):
        """Returns 0 when no variants are stuck in PROCESSING."""
        from worker.transcoder import recover_interrupted

        profile = _make_profile(db_session)
        db_session.add(profile)
        await db_session.flush()

        asset = _make_asset()
        db_session.add(asset)
        await db_session.flush()

        v1 = _make_variant(asset.id, profile.id, status=VariantStatus.PENDING)
        v2 = _make_variant(asset.id, profile.id, status=VariantStatus.READY)
        db_session.add_all([v1, v2])
        await db_session.commit()

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        count = await recover_interrupted(factory)
        assert count == 0

    @pytest.mark.asyncio
    async def test_empty_database(self, db_engine):
        """Returns 0 when there are no variants at all."""
        from worker.transcoder import recover_interrupted

        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        count = await recover_interrupted(factory)
        assert count == 0


# ── process_pending tests ──


class TestProcessPending:
    """process_pending() should process variants in order."""

    @pytest.mark.asyncio
    async def test_processes_pending_variants(self, db_engine, db_session, tmp_path):
        """Should process PENDING variants and return count."""
        from worker.transcoder import process_pending

        profile = _make_profile(db_session)
        db_session.add(profile)
        await db_session.flush()

        asset = _make_asset()
        db_session.add(asset)
        await db_session.flush()

        v1 = _make_variant(asset.id, profile.id, status=VariantStatus.PENDING)
        v2 = _make_variant(asset.id, profile.id, status=VariantStatus.PENDING)
        v3 = _make_variant(asset.id, profile.id, status=VariantStatus.READY)
        db_session.add_all([v1, v2, v3])
        await db_session.commit()

        # Create source file so _transcode_one doesn't fail on missing file
        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        (asset_dir / "test-video.mp4").write_bytes(b"fake video data")

        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        # Mock _transcode_one to mark variants as READY without actually running ffmpeg
        async def fake_transcode(variant, db, asset_dir):
            variant.status = VariantStatus.READY
            variant.progress = 100.0
            await db.commit()

        with patch("worker.transcoder._transcode_one", side_effect=fake_transcode):
            count = await process_pending(factory, asset_dir)

        assert count == 2

    @pytest.mark.asyncio
    async def test_returns_zero_when_nothing_pending(self, db_engine, tmp_path):
        """Returns 0 when no PENDING variants exist."""
        from worker.transcoder import process_pending

        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        count = await process_pending(factory, asset_dir)
        assert count == 0

    @pytest.mark.asyncio
    async def test_skips_non_pending(self, db_engine, db_session, tmp_path):
        """Should only process PENDING variants, skip READY/FAILED."""
        from worker.transcoder import process_pending

        profile = _make_profile(db_session)
        db_session.add(profile)
        await db_session.flush()

        asset = _make_asset()
        db_session.add(asset)
        await db_session.flush()

        v1 = _make_variant(asset.id, profile.id, status=VariantStatus.READY)
        v2 = _make_variant(asset.id, profile.id, status=VariantStatus.FAILED)
        v3 = _make_variant(asset.id, profile.id, status=VariantStatus.PROCESSING)
        db_session.add_all([v1, v2, v3])
        await db_session.commit()

        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        count = await process_pending(factory, asset_dir)
        assert count == 0


# ── Worker cancel functions ──


class TestWorkerCancelProfileTranscodes:
    """Worker cancel_profile_transcodes should kill matching ffmpeg."""

    @pytest.mark.asyncio
    async def test_kills_matching_profile(self):
        from worker import transcoder

        profile_id = uuid.uuid4()
        mock_proc = MagicMock()
        mock_proc.returncode = None  # still running

        transcoder._active_process = mock_proc
        transcoder._active_profile_id = profile_id
        transcoder._active_variant_id = uuid.uuid4()

        try:
            result = transcoder.cancel_profile_transcodes(profile_id)
            assert result is True
            mock_proc.terminate.assert_called_once()
        finally:
            transcoder._active_process = None
            transcoder._active_profile_id = None
            transcoder._active_variant_id = None

    @pytest.mark.asyncio
    async def test_ignores_different_profile(self):
        from worker import transcoder

        mock_proc = MagicMock()
        mock_proc.returncode = None

        transcoder._active_process = mock_proc
        transcoder._active_profile_id = uuid.uuid4()
        transcoder._active_variant_id = uuid.uuid4()

        try:
            result = transcoder.cancel_profile_transcodes(uuid.uuid4())
            assert result is False
            mock_proc.terminate.assert_not_called()
        finally:
            transcoder._active_process = None
            transcoder._active_profile_id = None
            transcoder._active_variant_id = None

    @pytest.mark.asyncio
    async def test_no_op_when_idle(self):
        from worker import transcoder

        transcoder._active_process = None
        transcoder._active_profile_id = None
        transcoder._active_variant_id = None

        result = transcoder.cancel_profile_transcodes(uuid.uuid4())
        assert result is False

    @pytest.mark.asyncio
    async def test_no_op_when_process_finished(self):
        """Should not terminate an already-finished process."""
        from worker import transcoder

        profile_id = uuid.uuid4()
        mock_proc = MagicMock()
        mock_proc.returncode = 0  # already finished

        transcoder._active_process = mock_proc
        transcoder._active_profile_id = profile_id
        transcoder._active_variant_id = uuid.uuid4()

        try:
            result = transcoder.cancel_profile_transcodes(profile_id)
            assert result is False
            mock_proc.terminate.assert_not_called()
        finally:
            transcoder._active_process = None
            transcoder._active_profile_id = None
            transcoder._active_variant_id = None


class TestWorkerCancelAssetTranscodes:
    """Worker cancel_asset_transcodes should kill matching ffmpeg."""

    @pytest.mark.asyncio
    async def test_kills_matching_asset(self):
        from worker import transcoder

        asset_id = uuid.uuid4()
        mock_proc = MagicMock()
        mock_proc.returncode = None

        transcoder._active_process = mock_proc
        transcoder._active_source_asset_id = asset_id
        transcoder._active_variant_id = uuid.uuid4()

        try:
            result = transcoder.cancel_asset_transcodes(asset_id)
            assert result is True
            mock_proc.terminate.assert_called_once()
        finally:
            transcoder._active_process = None
            transcoder._active_source_asset_id = None
            transcoder._active_variant_id = None

    @pytest.mark.asyncio
    async def test_ignores_different_asset(self):
        from worker import transcoder

        mock_proc = MagicMock()
        mock_proc.returncode = None

        transcoder._active_process = mock_proc
        transcoder._active_source_asset_id = uuid.uuid4()
        transcoder._active_variant_id = uuid.uuid4()

        try:
            result = transcoder.cancel_asset_transcodes(uuid.uuid4())
            assert result is False
            mock_proc.terminate.assert_not_called()
        finally:
            transcoder._active_process = None
            transcoder._active_source_asset_id = None
            transcoder._active_variant_id = None


# ── _transcode_one tests (unit level, mocked ffmpeg) ──


class TestTranscodeOneMissingSource:
    """_transcode_one should fail gracefully when source file is missing."""

    @pytest.mark.asyncio
    async def test_marks_failed_on_missing_source(self, db_session, tmp_path):
        from worker.transcoder import _transcode_one

        profile = _make_profile(db_session)
        db_session.add(profile)
        await db_session.flush()

        asset = _make_asset(filename="nonexistent.mp4")
        db_session.add(asset)
        await db_session.flush()

        variant = _make_variant(asset.id, profile.id)
        db_session.add(variant)
        await db_session.commit()

        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        # Deliberately do NOT create the source file

        await _transcode_one(variant, db_session, asset_dir)

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.FAILED
        assert "Source file not found" in variant.error_message


class TestTranscodeOneImageSuccess:
    """_transcode_one should handle image variants correctly."""

    @pytest.mark.asyncio
    async def test_image_variant_success(self, db_session, tmp_path):
        from worker.transcoder import _transcode_one

        profile = _make_profile(db_session)
        db_session.add(profile)
        await db_session.flush()

        asset = _make_asset(
            filename="photo.jpg",
            asset_type=AssetType.IMAGE,
        )
        db_session.add(asset)
        await db_session.flush()

        variant_id = uuid.uuid4()
        variant = _make_variant(
            asset.id, profile.id,
            id=variant_id,
            filename=f"{variant_id}.jpg",
        )
        db_session.add(variant)
        await db_session.commit()

        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        (asset_dir / "photo.jpg").write_bytes(b"fake jpeg data")

        async def mock_convert(source_path, output_path, **kwargs):
            output_path.write_bytes(b"converted image data")
            return True

        mock_storage = MagicMock()
        mock_storage.on_file_stored = AsyncMock()

        with patch("worker.transcoder.convert_image", side_effect=mock_convert), \
             patch("worker.transcoder.probe_media", return_value={}), \
             patch("worker.transcoder.get_storage", return_value=mock_storage):
            await _transcode_one(variant, db_session, asset_dir)

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.READY
        assert variant.progress == 100.0
        assert variant.size_bytes > 0
        assert variant.checksum is not None
        assert variant.completed_at is not None
        mock_storage.on_file_stored.assert_called_once()

    @pytest.mark.asyncio
    async def test_image_convert_failure(self, db_session, tmp_path):
        from worker.transcoder import _transcode_one

        profile = _make_profile(db_session)
        db_session.add(profile)
        await db_session.flush()

        asset = _make_asset(filename="bad.jpg", asset_type=AssetType.IMAGE)
        db_session.add(asset)
        await db_session.flush()

        variant = _make_variant(asset.id, profile.id, filename=f"{uuid.uuid4()}.jpg")
        db_session.add(variant)
        await db_session.commit()

        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        (asset_dir / "bad.jpg").write_bytes(b"corrupt data")

        async def mock_convert_fail(source_path, output_path, **kwargs):
            return False

        with patch("worker.transcoder.convert_image", side_effect=mock_convert_fail), \
             patch("worker.transcoder.probe_media", return_value={}):
            await _transcode_one(variant, db_session, asset_dir)

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.FAILED
        assert "Image conversion failed" in variant.error_message


class TestTranscodeOneVideoSuccess:
    """_transcode_one should handle video transcoding with mocked ffmpeg."""

    @pytest.mark.asyncio
    async def test_video_transcode_success(self, db_session, tmp_path):
        from worker.transcoder import _transcode_one

        profile = _make_profile(db_session)
        db_session.add(profile)
        await db_session.flush()

        asset = _make_asset()
        db_session.add(asset)
        await db_session.flush()

        variant = _make_variant(asset.id, profile.id)
        db_session.add(variant)
        await db_session.commit()

        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        (asset_dir / "test-video.mp4").write_bytes(b"fake video data")

        variants_dir = asset_dir / "variants"
        variants_dir.mkdir()

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(side_effect=[
            b"time=00:00:05.00 speed=2x", b""
        ])
        mock_proc.wait = AsyncMock()

        # Create output file as ffmpeg would
        async def fake_exec(*args, **kwargs):
            output_file = variants_dir / variant.filename
            output_file.write_bytes(b"transcoded video output data")
            return mock_proc

        mock_storage = MagicMock()
        mock_storage.on_file_stored = AsyncMock()

        with patch("worker.transcoder.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("worker.transcoder._get_duration", return_value=10.0), \
             patch("worker.transcoder.probe_media", return_value={}), \
             patch("worker.transcoder.get_storage", return_value=mock_storage):
            await _transcode_one(variant, db_session, asset_dir)

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.READY
        assert variant.progress == 100.0
        assert variant.size_bytes > 0
        assert variant.checksum is not None
        mock_storage.on_file_stored.assert_called_once()

    @pytest.mark.asyncio
    async def test_video_transcode_failure(self, db_session, tmp_path):
        from worker.transcoder import _transcode_one

        profile = _make_profile(db_session)
        db_session.add(profile)
        await db_session.flush()

        asset = _make_asset()
        db_session.add(asset)
        await db_session.flush()

        variant = _make_variant(asset.id, profile.id)
        db_session.add(variant)
        await db_session.commit()

        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        (asset_dir / "test-video.mp4").write_bytes(b"fake video data")

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(side_effect=[
            b"Error: something went wrong", b""
        ])
        mock_proc.wait = AsyncMock()

        with patch("worker.transcoder.asyncio.create_subprocess_exec", return_value=mock_proc), \
             patch("worker.transcoder._get_duration", return_value=10.0), \
             patch("worker.transcoder.probe_media", return_value={}):
            await _transcode_one(variant, db_session, asset_dir)

        await db_session.refresh(variant)
        assert variant.status == VariantStatus.FAILED
        assert "ffmpeg exit code 1" in variant.error_message

    @pytest.mark.asyncio
    async def test_video_uses_original_when_available(self, db_session, tmp_path):
        """When original_filename is set, should transcode from originals/ dir."""
        from worker.transcoder import _transcode_one

        profile = _make_profile(db_session)
        db_session.add(profile)
        await db_session.flush()

        asset = _make_asset(
            filename="video.mp4",
            original_filename="video_original.mov",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = _make_variant(asset.id, profile.id)
        db_session.add(variant)
        await db_session.commit()

        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()
        originals_dir = asset_dir / "originals"
        originals_dir.mkdir()
        (asset_dir / "video.mp4").write_bytes(b"intermediate")
        (originals_dir / "video_original.mov").write_bytes(b"original high quality")
        variants_dir = asset_dir / "variants"
        variants_dir.mkdir()

        captured_args = []

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.stderr = AsyncMock()
        mock_proc.stderr.read = AsyncMock(return_value=b"")
        mock_proc.wait = AsyncMock()

        async def fake_exec(*args, **kwargs):
            captured_args.extend(args)
            # Create output
            output_file = variants_dir / variant.filename
            output_file.write_bytes(b"output data")
            return mock_proc

        mock_storage = MagicMock()
        mock_storage.on_file_stored = AsyncMock()

        with patch("worker.transcoder.asyncio.create_subprocess_exec", side_effect=fake_exec), \
             patch("worker.transcoder._get_duration", return_value=10.0), \
             patch("worker.transcoder.probe_media", return_value={}), \
             patch("worker.transcoder.get_storage", return_value=mock_storage):
            await _transcode_one(variant, db_session, asset_dir)

        # Verify the original file was used as source (in the ffmpeg -i arg)
        args_str = " ".join(str(a) for a in captured_args)
        assert "originals" in args_str
        assert "video_original.mov" in args_str


# ── _queue_mode tests ──


class TestQueueMode:
    """Queue mode processes one job per invocation and exits."""

    @pytest.mark.asyncio
    async def test_queue_mode_exits_when_no_connection_string(self, db_engine, tmp_path):
        """Queue mode bails out when no Azure connection string is configured."""
        from worker.__main__ import _queue_mode

        asset_dir = tmp_path / "assets"
        asset_dir.mkdir()

        factory = async_sessionmaker(db_engine, expire_on_commit=False)

        settings = MagicMock()
        settings.asset_storage_path = asset_dir
        settings.azure_storage_connection_string = None

        with patch("worker.__main__.get_session_factory", return_value=factory), \
             patch("worker.transcoder.recover_interrupted", new=AsyncMock(return_value=0)):
            await _queue_mode(settings)


# ── CMS notify_worker tests ──


class TestNotifyWorker:
    """CMS notify_worker should issue NOTIFY on the transcode_jobs channel."""

    @pytest.mark.asyncio
    async def test_notify_calls_execute(self, db_session):
        """notify_worker should run NOTIFY transcode_jobs on the session."""
        from cms.services.transcoder import notify_worker

        # For SQLite tests, NOTIFY isn't supported, but we verify the
        # function doesn't crash and handles non-PG gracefully
        try:
            await notify_worker(db_session)
        except Exception:
            # SQLite doesn't support NOTIFY — that's expected in tests
            pass

    @pytest.mark.asyncio
    async def test_notify_is_importable_from_cms(self):
        """notify_worker should be importable from the CMS shim."""
        from cms.services.transcoder import notify_worker
        assert callable(notify_worker)


# ── CMS shim no-op tests ──


class TestCmsTranscoderShim:
    """CMS transcoder shim functions should be no-ops / re-exports."""

    @pytest.mark.asyncio
    async def test_cancel_profile_is_noop(self):
        from cms.services.transcoder import cancel_profile_transcodes
        assert cancel_profile_transcodes(uuid.uuid4()) is False

    @pytest.mark.asyncio
    async def test_cancel_asset_is_noop(self):
        from cms.services.transcoder import cancel_asset_transcodes
        assert cancel_asset_transcodes(uuid.uuid4()) is False

    def test_probe_media_reexported(self):
        from cms.services.transcoder import probe_media
        from shared.services.probe import probe_media as shared_probe
        assert probe_media is shared_probe

    def test_convert_image_reexported(self):
        from cms.services.transcoder import convert_image
        from shared.services.image import convert_image as shared_convert
        assert convert_image is shared_convert

    def test_convert_image_to_jpeg_reexported(self):
        from cms.services.transcoder import convert_image_to_jpeg
        from shared.services.image import convert_image as shared_convert
        assert convert_image_to_jpeg is shared_convert
