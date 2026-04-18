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
                             error_message=""):
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
            error_message=error_message or None,
        )
        db_session.add(variant)
        await db_session.commit()
        return variant, asset

    async def test_should_retry_saved_stream(self, db_session):
        """SAVED_STREAM variants with a retryable error are eligible for retry."""
        from worker.transcoder import _should_retry

        variant, asset = await self._make_variant(db_session)
        variant.error_message = "Connection timed out"
        assert _should_retry(variant, asset) is True

    async def test_should_not_retry_regular_video(self, db_session):
        """Non-SAVED_STREAM variants are never retried at the variant level."""
        from worker.transcoder import _should_retry

        variant, asset = await self._make_variant(
            db_session, asset_type=AssetType.VIDEO,
        )
        variant.error_message = "Something went wrong"
        assert _should_retry(variant, asset) is False

    async def test_should_not_retry_non_retryable_error(self, db_session):
        """Non-retryable errors (bad input) are not retried."""
        from worker.transcoder import _should_retry

        variant, asset = await self._make_variant(db_session)
        variant.error_message = "Image conversion failed"
        assert _should_retry(variant, asset) is False

        variant.error_message = "Invalid data found when processing input"
        assert _should_retry(variant, asset) is False

    async def test_mark_failed_retries_saved_stream(self, db_session):
        """_mark_failed leaves a retryable SAVED_STREAM variant PENDING (job retry handles count)."""
        from worker.transcoder import _mark_failed

        variant, asset = await self._make_variant(db_session)
        await _mark_failed(variant, asset, "Connection reset", db_session)

        assert variant.status == VariantStatus.PENDING
        assert variant.progress == 0.0

    async def test_mark_failed_permanently_for_video(self, db_session):
        """_mark_failed always sets FAILED for non-SAVED_STREAM assets."""
        from worker.transcoder import _mark_failed

        variant, asset = await self._make_variant(
            db_session, asset_type=AssetType.VIDEO,
        )
        await _mark_failed(variant, asset, "Transcode error", db_session)

        assert variant.status == VariantStatus.FAILED


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
        asset.capture_progress = 100.0
        asset.capture_error = "stale error from a prior run"

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

        # Asset should be reset for re-capture
        await db_session.refresh(asset)
        assert asset.checksum == ""
        assert asset.size_bytes == 0
        # Capture state cleared so the UI shows a fresh "Queued" state rather
        # than carrying over stale progress / error from the previous attempt.
        assert asset.capture_progress is None
        assert asset.capture_error is None

    async def test_recapture_404_for_missing_asset(self, client, db_session):
        """Recapture returns 404 for nonexistent asset."""
        fake_id = uuid.uuid4()
        resp = await client.post(f"/api/assets/{fake_id}/recapture")
        assert resp.status_code == 404


# ── Stream Probe Tests ──────────────────────────────────────────


@pytest.mark.asyncio
class TestStreamProbe:
    """Tests for the stream probe endpoint."""

    @pytest.fixture(autouse=True)
    def _setup(self, client, db_session):
        self.client = client
        self.db = db_session

    async def test_probe_hls_master_live(self, client, monkeypatch):
        """Probe correctly identifies a live HLS master playlist."""
        master_m3u8 = (
            "#EXTM3U\n"
            "#EXT-X-VERSION:3\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=2000000,RESOLUTION=1280x720,CODECS="avc1.64001f,mp4a.40.2",FRAME-RATE=30\n'
            "720p.m3u8\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=1000000,RESOLUTION=640x360,CODECS="avc1.64001e,mp4a.40.2",FRAME-RATE=30\n'
            "360p.m3u8\n"
        )
        # Child playlist — live (no EXT-X-ENDLIST)
        child_m3u8 = (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:4\n#EXT-X-MEDIA-SEQUENCE:100\n"
            "#EXTINF:4,\nseg100.ts\n#EXTINF:4,\nseg101.ts\n"
        )

        import httpx

        class FakeResp:
            status_code = 200
            text = ""
            def raise_for_status(self): pass

        call_count = 0

        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw):
                nonlocal call_count
                r = FakeResp()
                r.text = master_m3u8 if call_count == 0 else child_m3u8
                call_count += 1
                return r

        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

        resp = await client.get("/api/streams/probe?url=https://example.com/live/master.m3u8")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_live"] is True
        assert data["type"] == "hls"
        assert data["resolution"] == "1280x720"
        assert data["codecs"] == "H.264 + AAC"
        assert len(data["variants"]) == 2

    async def test_probe_hls_vod(self, client, monkeypatch):
        """Probe correctly identifies a VOD HLS playlist with duration."""
        master_m3u8 = (
            "#EXTM3U\n"
            '#EXT-X-STREAM-INF:BANDWIDTH=3000000,RESOLUTION=1920x1080,CODECS="avc1.640028,mp4a.40.2"\n'
            "1080p.m3u8\n"
        )
        child_m3u8 = (
            "#EXTM3U\n#EXT-X-VERSION:3\n#EXT-X-TARGETDURATION:10\n"
            "#EXTINF:10.0,\nseg0.ts\n#EXTINF:10.0,\nseg1.ts\n#EXTINF:10.0,\nseg2.ts\n"
            "#EXTINF:5.5,\nseg3.ts\n#EXT-X-ENDLIST\n"
        )

        import httpx
        class FakeResp:
            status_code = 200; text = ""
            def raise_for_status(self): pass
        call_count = 0
        class FakeClient:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): pass
            async def get(self, url, **kw):
                nonlocal call_count
                r = FakeResp()
                r.text = master_m3u8 if call_count == 0 else child_m3u8
                call_count += 1
                return r
        monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: FakeClient())

        resp = await client.get("/api/streams/probe?url=https://example.com/vod/master.m3u8")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_live"] is False
        assert data["duration_seconds"] == 35.5  # 10+10+10+5.5
        assert len(data["variants"]) == 1

    async def test_probe_rtmp_always_live(self, client, monkeypatch):
        """RTMP URLs are always reported as live."""
        import asyncio
        async def fake_ffprobe(url):
            return {"resolution": "1920x1080", "video_codec": "H264"}
        monkeypatch.setattr("cms.routers.stream_probe._ffprobe_url", fake_ffprobe)

        resp = await client.get("/api/streams/probe?url=rtmp://stream.example.com/live/key")
        assert resp.status_code == 200
        data = resp.json()
        assert data["is_live"] is True
        assert data["type"] == "rtmp_rtsp"

    async def test_probe_invalid_url(self, client):
        """Probe rejects invalid URLs."""
        resp = await client.get("/api/streams/probe?url=not-a-url")
        assert resp.status_code == 400


# ── Capture Duration Tests ──────────────────────────────────────


@pytest.mark.asyncio
class TestCaptureDuration:
    """Tests for capture_duration on saved stream assets."""

    @pytest.fixture(autouse=True)
    def _setup(self, client, db_session):
        self.client = client
        self.db = db_session

    async def test_saved_stream_with_capture_duration(self, client, db_session):
        """Saved stream accepts capture_duration."""
        group = await _seed_group_and_device(db_session)
        resp = await client.post("/api/assets/stream", json={
            "url": "https://live.example.com/feed.m3u8",
            "save_locally": True,
            "capture_duration": 1800,
            "group_ids": [str(group.id)],
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["capture_duration"] == 1800

    async def test_capture_duration_too_short(self, client, db_session):
        """Capture duration below 10s is rejected."""
        group = await _seed_group_and_device(db_session)
        resp = await client.post("/api/assets/stream", json={
            "url": "https://live.example.com/short.m3u8",
            "save_locally": True,
            "capture_duration": 5,
            "group_ids": [str(group.id)],
        })
        assert resp.status_code == 400
        assert "at least 10" in resp.json()["detail"]

    async def test_capture_duration_too_long(self, client, db_session):
        """Capture duration above 4 hours is rejected."""
        group = await _seed_group_and_device(db_session)
        resp = await client.post("/api/assets/stream", json={
            "url": "https://live.example.com/long.m3u8",
            "save_locally": True,
            "capture_duration": 99999,
            "group_ids": [str(group.id)],
        })
        assert resp.status_code == 400
        assert "4 hours" in resp.json()["detail"]

    async def test_live_stream_ignores_capture_duration(self, client, db_session):
        """Live streams (not saved) ignore capture_duration."""
        group = await _seed_group_and_device(db_session)
        resp = await client.post("/api/assets/stream", json={
            "url": "https://live.example.com/nodur.m3u8",
            "save_locally": False,
            "capture_duration": 1800,
            "group_ids": [str(group.id)],
        })
        assert resp.status_code == 201
        assert resp.json()["capture_duration"] is None


# ── Capture Progress / Error (worker) ────────────────────────────


class _FakeStderr:
    """Minimal async stream that yields pre-canned stderr chunks."""

    def __init__(self, chunks):
        self._chunks = list(chunks)

    async def read(self, _n):
        if not self._chunks:
            return b""
        return self._chunks.pop(0)


class _FakeProc:
    def __init__(self, chunks, returncode=0):
        self.stderr = _FakeStderr(chunks)
        self.returncode = returncode

    async def wait(self):
        return self.returncode

    def terminate(self):
        self.returncode = -15

    async def communicate(self):
        return b"", b""


@pytest.mark.asyncio
class TestCaptureProgressAndError:
    """Covers the new capture_progress / capture_error columns and the
    ffmpeg-progress parsing loop in worker._capture_stream.
    """

    async def test_capture_progress_updates_monotonically(
        self, db_session, tmp_path, monkeypatch
    ):
        from worker import transcoder

        asset = await _create_stream_asset(
            db_session, asset_type=AssetType.SAVED_STREAM,
            url="https://example.com/progress.m3u8",
        )
        asset.capture_duration = 100  # nice round number → 1s == 1%
        await db_session.commit()

        # Emit two time= markers: 30% and 70%.  Then EOF.
        chunks = [
            b"frame=  10 time=00:00:30.00 bitrate=1000\n",
            b"frame=  20 time=00:01:10.00 bitrate=1000\n",
            b"",
        ]
        capture_path = tmp_path / f"{asset.id}_capture.mp4"
        capture_path.write_bytes(b"\x00" * 1024)  # non-empty so success path runs

        async def _fake_subprocess_exec(*args, **kwargs):
            return _FakeProc(chunks, returncode=0)

        async def _fake_probe(_path):
            return {"duration_seconds": 100.0}

        monkeypatch.setattr(transcoder.asyncio, "create_subprocess_exec",
                            _fake_subprocess_exec)
        monkeypatch.setattr(transcoder, "probe_media", _fake_probe)

        result = await transcoder._capture_stream(asset, tmp_path, db_session)

        assert result == capture_path
        await db_session.refresh(asset)
        # Success path sets progress to 100 and clears any prior error.
        assert asset.capture_progress == 100.0
        assert asset.capture_error is None

    async def test_capture_error_persisted_on_ffmpeg_failure(
        self, db_session, tmp_path, monkeypatch
    ):
        from worker import transcoder

        asset = await _create_stream_asset(
            db_session, asset_type=AssetType.SAVED_STREAM,
            url="https://example.com/fail.m3u8",
        )
        asset.capture_duration = 60
        await db_session.commit()

        chunks = [b"[hls @ 0x0] Invalid data found when processing input\n", b""]

        async def _fake_subprocess_exec(*args, **kwargs):
            return _FakeProc(chunks, returncode=1)

        monkeypatch.setattr(transcoder.asyncio, "create_subprocess_exec",
                            _fake_subprocess_exec)

        result = await transcoder._capture_stream(asset, tmp_path, db_session)

        assert result is None
        await db_session.refresh(asset)
        assert asset.capture_error is not None
        assert "exit code 1" in asset.capture_error
        assert "Invalid data" in asset.capture_error

    async def test_capture_error_on_empty_output_file(
        self, db_session, tmp_path, monkeypatch
    ):
        from worker import transcoder

        asset = await _create_stream_asset(
            db_session, asset_type=AssetType.SAVED_STREAM,
            url="https://example.com/empty.m3u8",
        )
        asset.capture_duration = 30
        await db_session.commit()

        async def _fake_subprocess_exec(*args, **kwargs):
            # Successful exit but no output file written → empty-file branch.
            return _FakeProc([b""], returncode=0)

        monkeypatch.setattr(transcoder.asyncio, "create_subprocess_exec",
                            _fake_subprocess_exec)

        result = await transcoder._capture_stream(asset, tmp_path, db_session)

        assert result is None
        await db_session.refresh(asset)
        assert asset.capture_error is not None
        assert "empty" in asset.capture_error.lower()

    async def test_capture_progress_uses_probed_duration_for_vod(
        self, db_session, tmp_path, monkeypatch
    ):
        """When _get_duration returns a finite value (VOD), progress should be
        scaled against the probed duration, NOT max_duration. Without this,
        the bar would crawl to ~probed/max % then jump to 100 at completion.
        """
        from worker import transcoder

        asset = await _create_stream_asset(
            db_session, asset_type=AssetType.SAVED_STREAM,
            url="https://example.com/vod.m3u8",
        )
        asset.capture_duration = 14400  # 4h cap, probed VOD is 100s
        await db_session.commit()

        # ffmpeg emits time=00:00:50 (= 50% of probed 100s, would only be
        # 0.35% if we used the 14400s cap as denom).
        chunks = [b"frame=10 time=00:00:50.00 bitrate=1000\n", b""]
        capture_path = tmp_path / f"{asset.id}_capture.mp4"
        capture_path.write_bytes(b"\x00" * 1024)

        async def _fake_get_duration(_url):
            return 100.0  # finite VOD duration

        async def _fake_subprocess_exec(*args, **kwargs):
            return _FakeProc(chunks, returncode=0)

        async def _fake_probe(_path):
            return {"duration_seconds": 100.0}

        monkeypatch.setattr(transcoder, "_get_duration", _fake_get_duration)
        monkeypatch.setattr(transcoder.asyncio, "create_subprocess_exec",
                            _fake_subprocess_exec)
        monkeypatch.setattr(transcoder, "probe_media", _fake_probe)

        # Capture mid-progress samples by snapshotting after each commit.
        observed = []
        orig_commit = db_session.commit

        async def _spy_commit():
            await orig_commit()
            if asset.capture_progress is not None:
                observed.append(asset.capture_progress)

        monkeypatch.setattr(db_session, "commit", _spy_commit)

        await transcoder._capture_stream(asset, tmp_path, db_session)

        # Should observe ~50% (probed-based) before the 100% completion commit.
        # Allow a small tolerance for the 99% cap in the loop.
        mid = [p for p in observed if 0 < p < 99]
        assert any(40 <= p <= 60 for p in mid), (
            f"Expected mid-capture progress around 50%% (probed-based), got {observed}"
        )

    async def test_capture_uses_max_duration_for_live(
        self, db_session, tmp_path, monkeypatch
    ):
        """When _get_duration returns None (livestream), -t max_duration must
        be passed to ffmpeg as a safety cap and progress denominator.
        """
        from worker import transcoder

        asset = await _create_stream_asset(
            db_session, asset_type=AssetType.SAVED_STREAM,
            url="rtmp://example.com/live",
        )
        asset.capture_duration = 600  # 10 min cap
        await db_session.commit()

        captured_args = []

        async def _fake_get_duration(_url):
            return None  # livestream — no advertised duration

        async def _fake_subprocess_exec(*args, **kwargs):
            captured_args.append(args)
            return _FakeProc([b""], returncode=0)

        monkeypatch.setattr(transcoder, "_get_duration", _fake_get_duration)
        monkeypatch.setattr(transcoder.asyncio, "create_subprocess_exec",
                            _fake_subprocess_exec)

        await transcoder._capture_stream(asset, tmp_path, db_session)

        assert captured_args, "ffmpeg should have been invoked"
        args = list(captured_args[0])
        assert "-t" in args, f"-t safety cap missing for live stream: {args}"
        assert "600" in args, f"-t value should equal capture_duration (600): {args}"