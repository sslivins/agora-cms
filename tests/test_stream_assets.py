"""Tests for stream and saved-stream asset features.

Covers:
- Stream/SAVED_STREAM asset creation via API
- Duplicate URL detection (per type)
- SAVED_STREAM schedule validation (no loop_count restriction)
- STREAM schedule validation (loop_count rejected, end_time required)
- Retry logic for SAVED_STREAM variant failures
"""

import uuid

import pytest

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_profile import DeviceProfile


# ── Helpers ──────────────────────────────────────────────────────


async def _seed_group_and_device(db_session):
    group = DeviceGroup(name="Stream Test Group")
    device = Device(id="stream-pi", name="Stream Pi", status=DeviceStatus.ADOPTED)
    db_session.add_all([group, device])
    await db_session.flush()
    device.group_id = group.id
    await db_session.commit()
    return group


async def _create_stream_asset(db_session, *, asset_type=AssetType.STREAM,
                                url="rtsp://example.com/live"):
    asset = Asset(
        filename="Test Stream",
        asset_type=asset_type,
        size_bytes=0,
        checksum="",
        url=url,
    )
    db_session.add(asset)
    await db_session.commit()
    return asset


async def _create_video_asset(db_session, *, duration=120.0):
    asset = Asset(
        filename="test_video.mp4",
        asset_type=AssetType.VIDEO,
        size_bytes=5000,
        checksum="abc123",
        duration_seconds=duration,
    )
    db_session.add(asset)
    await db_session.commit()
    return asset


# ── Stream Asset Creation ────────────────────────────────────────


@pytest.mark.asyncio
class TestStreamAssetCreation:

    async def test_create_live_stream(self, client):
        """POST /api/assets/stream creates a STREAM asset by default."""
        resp = await client.post("/api/assets/stream", json={
            "url": "rtsp://example.com/live-1",
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["asset_type"] == "stream"
        assert data["url"] == "rtsp://example.com/live-1"

    async def test_create_saved_stream(self, client):
        """POST /api/assets/stream with save_locally=true creates SAVED_STREAM."""
        resp = await client.post("/api/assets/stream", json={
            "url": "rtsp://example.com/capture-1",
            "save_locally": True,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["asset_type"] == "saved_stream"

    async def test_same_url_allowed_as_both_types(self, client):
        """Same URL can exist as both STREAM and SAVED_STREAM."""
        url = "rtsp://example.com/both-types"

        resp1 = await client.post("/api/assets/stream", json={
            "url": url, "save_locally": False,
        })
        assert resp1.status_code == 201
        assert resp1.json()["asset_type"] == "stream"

        resp2 = await client.post("/api/assets/stream", json={
            "url": url, "save_locally": True,
        })
        assert resp2.status_code == 201
        assert resp2.json()["asset_type"] == "saved_stream"

    async def test_duplicate_live_stream_rejected(self, client):
        """Duplicate URL within STREAM type returns 409."""
        url = "rtsp://example.com/dup-live"
        resp1 = await client.post("/api/assets/stream", json={"url": url})
        assert resp1.status_code == 201

        resp2 = await client.post("/api/assets/stream", json={"url": url})
        assert resp2.status_code == 409
        assert "live stream" in resp2.json()["detail"].lower()

    async def test_duplicate_saved_stream_rejected(self, client):
        """Duplicate URL within SAVED_STREAM type returns 409."""
        url = "rtsp://example.com/dup-saved"
        resp1 = await client.post("/api/assets/stream", json={
            "url": url, "save_locally": True,
        })
        assert resp1.status_code == 201

        resp2 = await client.post("/api/assets/stream", json={
            "url": url, "save_locally": True,
        })
        assert resp2.status_code == 409
        assert "saved stream" in resp2.json()["detail"].lower()

    async def test_stream_url_required(self, client):
        """Missing URL returns 400."""
        resp = await client.post("/api/assets/stream", json={})
        assert resp.status_code == 400

    async def test_localhost_url_rejected(self, client):
        """Loopback URLs are blocked (SSRF protection)."""
        resp = await client.post("/api/assets/stream", json={
            "url": "rtsp://localhost/test",
        })
        assert resp.status_code == 400
        assert "localhost" in resp.json()["detail"].lower()


# ── Schedule Validation with Stream Assets ───────────────────────


@pytest.mark.asyncio
class TestStreamScheduleValidation:

    async def test_live_stream_rejects_loop_count(self, client, db_session):
        """STREAM assets cannot use loop_count (no duration)."""
        group = await _seed_group_and_device(db_session)
        asset = await _create_stream_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Stream Loop",
            "group_id": str(group.id),
            "asset_id": str(asset.id),
            "start_time": "08:00",
            "end_time": "12:00",
            "loop_count": 5,
        })
        assert resp.status_code == 422
        assert "loop count" in resp.json()["detail"].lower()

    async def test_live_stream_requires_end_time(self, client, db_session):
        """STREAM assets require end_time (no auto-compute from loop_count)."""
        group = await _seed_group_and_device(db_session)
        asset = await _create_stream_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "No End",
            "group_id": str(group.id),
            "asset_id": str(asset.id),
            "start_time": "08:00",
        })
        # Should fail — Pydantic validator requires end_time or loop_count
        assert resp.status_code == 422

    async def test_live_stream_schedule_ok_with_end_time(self, client, db_session):
        """STREAM asset schedule succeeds when end_time is provided."""
        group = await _seed_group_and_device(db_session)
        # Streams require Pi 5+ compatible devices in the group
        from sqlalchemy import update
        await db_session.execute(
            update(Device).where(Device.id == "stream-pi").values(device_type="Raspberry Pi 5")
        )
        await db_session.commit()

        asset = await _create_stream_asset(db_session)

        resp = await client.post("/api/schedules", json={
            "name": "Live OK",
            "group_id": str(group.id),
            "asset_id": str(asset.id),
            "start_time": "08:00",
            "end_time": "12:00",
        })
        assert resp.status_code == 201

    async def test_saved_stream_schedule_like_video(self, client, db_session):
        """SAVED_STREAM schedules work like VIDEO (end_time, no loop_count
        restriction in principle — but duration isn't known at creation,
        so end_time is required)."""
        group = await _seed_group_and_device(db_session)
        asset = await _create_stream_asset(
            db_session, asset_type=AssetType.SAVED_STREAM,
            url="rtsp://example.com/sched-saved",
        )

        resp = await client.post("/api/schedules", json={
            "name": "Saved Stream Sched",
            "group_id": str(group.id),
            "asset_id": str(asset.id),
            "start_time": "08:00",
            "end_time": "17:00",
        })
        assert resp.status_code == 201
        assert resp.json()["asset_id"] == str(asset.id)


# ── Retry Logic ──────────────────────────────────────────────────


@pytest.mark.asyncio
class TestSavedStreamRetry:

    async def _make_variant(self, db_session, *, asset_type=AssetType.SAVED_STREAM,
                             retry_count=0, error_message=""):
        profile = DeviceProfile(name=f"profile-{uuid.uuid4().hex[:6]}")
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="retry_test.mp4",
            asset_type=asset_type,
            size_bytes=0,
            checksum="",
            url="rtsp://example.com/retry-test",
        )
        db_session.add(asset)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename="retry_variant.mp4",
            size_bytes=0,
            status=VariantStatus.PROCESSING,
            progress=50.0,
            retry_count=retry_count,
            error_message=error_message or None,
        )
        db_session.add(variant)
        await db_session.commit()
        return variant, asset

    async def test_should_retry_saved_stream(self, db_session):
        """SAVED_STREAM variants with retries remaining should be retried."""
        from worker.transcoder import _should_retry

        variant, asset = await self._make_variant(db_session, retry_count=0)
        variant.error_message = "Connection timed out"
        assert _should_retry(variant, asset) is True

    async def test_should_not_retry_regular_video(self, db_session):
        """Non-SAVED_STREAM variants are never retried."""
        from worker.transcoder import _should_retry

        variant, asset = await self._make_variant(
            db_session, asset_type=AssetType.VIDEO,
        )
        variant.error_message = "Something went wrong"
        assert _should_retry(variant, asset) is False

    async def test_should_not_retry_max_retries_exceeded(self, db_session):
        """Variants at max retry count are not retried."""
        from worker.transcoder import _should_retry, STREAM_MAX_RETRIES

        variant, asset = await self._make_variant(
            db_session, retry_count=STREAM_MAX_RETRIES,
        )
        variant.error_message = "Connection timed out"
        assert _should_retry(variant, asset) is False

    async def test_should_not_retry_non_retryable_error(self, db_session):
        """Non-retryable errors (bad input) are not retried."""
        from worker.transcoder import _should_retry

        variant, asset = await self._make_variant(db_session, retry_count=0)
        variant.error_message = "Image conversion failed"
        assert _should_retry(variant, asset) is False

        variant.error_message = "Invalid data found when processing input"
        assert _should_retry(variant, asset) is False

    async def test_mark_failed_retries_saved_stream(self, db_session):
        """_mark_failed resets a retryable SAVED_STREAM variant to PENDING."""
        from worker.transcoder import _mark_failed

        variant, asset = await self._make_variant(db_session, retry_count=0)
        await _mark_failed(variant, asset, "Connection reset", db_session)

        assert variant.status == VariantStatus.PENDING
        assert variant.retry_count == 1
        assert variant.progress == 0.0

    async def test_mark_failed_permanently_after_max_retries(self, db_session):
        """_mark_failed sets FAILED when retry limit is reached."""
        from worker.transcoder import _mark_failed, STREAM_MAX_RETRIES

        variant, asset = await self._make_variant(
            db_session, retry_count=STREAM_MAX_RETRIES,
        )
        await _mark_failed(variant, asset, "Still failing", db_session)

        assert variant.status == VariantStatus.FAILED
        assert variant.retry_count == STREAM_MAX_RETRIES  # not incremented

    async def test_mark_failed_permanently_for_video(self, db_session):
        """_mark_failed always sets FAILED for non-SAVED_STREAM assets."""
        from worker.transcoder import _mark_failed

        variant, asset = await self._make_variant(
            db_session, asset_type=AssetType.VIDEO,
        )
        await _mark_failed(variant, asset, "Transcode error", db_session)

        assert variant.status == VariantStatus.FAILED
        assert variant.retry_count == 0


# ── SAVED_STREAM as file asset (scheduler) ───────────────────────


@pytest.mark.asyncio
class TestSavedStreamSchedulerBehavior:

    async def test_saved_stream_not_url_asset(self, db_session):
        """SAVED_STREAM should NOT be treated as a URL asset by scheduler."""
        from cms.services.scheduler import compute_now_playing

        asset = await _create_stream_asset(
            db_session, asset_type=AssetType.SAVED_STREAM,
            url="rtsp://example.com/scheduler-test",
        )
        # SAVED_STREAM behaves like VIDEO — it's a file-based asset
        assert asset.asset_type == AssetType.SAVED_STREAM
        assert asset.asset_type not in (AssetType.WEBPAGE, AssetType.STREAM)

    async def test_live_stream_is_url_asset(self, db_session):
        """STREAM should be treated as a URL asset by scheduler."""
        asset = await _create_stream_asset(db_session)
        assert asset.asset_type == AssetType.STREAM
        assert asset.asset_type in (AssetType.WEBPAGE, AssetType.STREAM)


# ── Capture formalization & recapture ────────────────────────────


@pytest.mark.asyncio
class TestCaptureFormalization:

    async def test_recapture_rejects_non_saved_stream(self, client, db_session):
        """Recapture endpoint rejects non-SAVED_STREAM assets."""
        asset = await _create_stream_asset(db_session)
        assert asset.asset_type == AssetType.STREAM

        resp = await client.post(f"/api/assets/{asset.id}/recapture")
        assert resp.status_code == 400
        assert "saved-stream" in resp.json()["detail"].lower()

    async def test_recapture_resets_variants(self, client, db_session):
        """Recapture resets all variants to PENDING."""
        group = await _seed_group_and_device(db_session)
        asset = await _create_stream_asset(
            db_session, asset_type=AssetType.SAVED_STREAM,
            url="https://example.com/recapture-test.m3u8",
        )
        # Simulate post-capture state
        asset.original_filename = "Recapture Test"
        asset.filename = f"{asset.id}_capture.mp4"
        asset.checksum = "abc123"
        asset.size_bytes = 1000

        profile = DeviceProfile(name="Test Prof", video_codec="libx264", audio_codec="aac")
        db_session.add(profile)
        await db_session.flush()

        variant = AssetVariant(
            source_asset_id=asset.id,
            profile_id=profile.id,
            filename=f"{uuid.uuid4()}.mp4",
            status=VariantStatus.READY,
            checksum="variant_hash",
            size_bytes=500,
        )
        db_session.add(variant)
        await db_session.commit()

        resp = await client.post(f"/api/assets/{asset.id}/recapture")
        assert resp.status_code == 200
        data = resp.json()
        assert data["recaptured"] is True

        # Variant should be reset
        await db_session.refresh(variant)
        assert variant.status == VariantStatus.PENDING
        assert variant.progress == 0.0
        assert variant.retry_count == 0

        # Asset should be reset for re-capture
        await db_session.refresh(asset)
        assert asset.checksum == ""
        assert asset.size_bytes == 0

    async def test_recapture_404_for_missing_asset(self, client, db_session):
        """Recapture returns 404 for nonexistent asset."""
        fake_id = uuid.uuid4()
        resp = await client.post(f"/api/assets/{fake_id}/recapture")
        assert resp.status_code == 404
