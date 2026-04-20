"""Tests for worker lifecycle robustness (PR follow-up to #265).

Covers:
- SIGTERM path: variant + job marked FAILED with timeout message; queue msg deleted.
- Lease-lost path: silent — no DB writes, no queue delete.
- Static invariants: MAX_JOB_RETRIES=3, VISIBILITY_TIMEOUT=60, replicaTimeout=7200.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

# Behavioural tests need `azure.storage.queue` (prod dep, may be absent locally).
try:
    import azure.storage.queue  # noqa: F401
    _HAS_AZURE_QUEUE = True
except ModuleNotFoundError:
    _HAS_AZURE_QUEUE = False

_requires_azure_queue = pytest.mark.skipif(
    not _HAS_AZURE_QUEUE,
    reason="azure-storage-queue not installed",
)

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.device_profile import DeviceProfile
from shared.models.job import Job, JobStatus, JobType, MAX_JOB_RETRIES


# ── Static invariants ──

class TestLifecycleConstants:
    """Constants wired by this PR — regressions here would re-open the incident."""

    def test_max_job_retries_is_three(self):
        # Lowered from 5 to 3: SIGTERM = terminal, so only transient failures
        # should ever retry. Three attempts are enough for a genuine transient.
        assert MAX_JOB_RETRIES == 3

    def test_visibility_timeout_is_sixty_seconds(self):
        """VISIBILITY_TIMEOUT (queue-mode lease window) must be >= 2 × heartbeat."""
        import worker.__main__ as wmain
        # Values are local to _queue_mode, so parse the source to pin them.
        src = Path(wmain.__file__).read_text(encoding="utf-8")
        assert "VISIBILITY_TIMEOUT = 60" in src
        assert "HEARTBEAT_INTERVAL = 15" in src

    def test_bicep_replica_timeout_is_two_hours(self):
        """Infra: Container App Jobs replicaTimeout must be 7200s (2 h)."""
        root = Path(__file__).resolve().parents[1]
        bicep = (root / "infra" / "modules" / "containerApps.bicep").read_text(encoding="utf-8")
        assert "replicaTimeout: 7200" in bicep
        assert "replicaTimeout: 1800" not in bicep


# ── SIGTERM + lease-loss behavioural tests ──


def _make_queue_msg(content: str) -> MagicMock:
    msg = MagicMock()
    msg.content = content
    msg.pop_receipt = "pr-initial"
    return msg


def _make_queue_client() -> MagicMock:
    qc = MagicMock()
    qc.update_message.return_value = MagicMock(pop_receipt="pr-refreshed")
    qc.delete_message = MagicMock()
    return qc


async def _seed_asset_variant_job(db_session):
    """Create a complete (profile, asset, variant, job) chain and return the ids."""
    profile = DeviceProfile(
        name=f"prof-{uuid.uuid4().hex[:6]}",
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
        filename="src.mp4",
        asset_type=AssetType.VIDEO,
        size_bytes=1000,
        checksum="abc123",
    )
    db_session.add(asset)
    await db_session.flush()

    variant = AssetVariant(
        id=uuid.uuid4(),
        source_asset_id=asset.id,
        profile_id=profile.id,
        filename=f"{uuid.uuid4()}.mp4",
        status=VariantStatus.PENDING,
    )
    db_session.add(variant)
    await db_session.flush()

    job = Job(
        id=uuid.uuid4(),
        type=JobType.VARIANT_TRANSCODE,
        target_id=variant.id,
        status=JobStatus.PENDING,
        retry_count=0,
    )
    db_session.add(job)
    await db_session.commit()
    return job.id, variant.id


@_requires_azure_queue
@pytest.mark.asyncio
async def test_sigterm_path_marks_failed_and_deletes_message(db_engine, tmp_path, monkeypatch):
    """On SIGTERM (replicaTimeout), worker marks variant+job FAILED and deletes the queue msg.

    Guarantees: the doomed transcode does NOT retry (queue msg gone) and the user
    sees a plain-English error in the UI.
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        job_id, variant_id = await _seed_asset_variant_job(db)

    queue_msg = _make_queue_msg(str(job_id))
    queue_client = _make_queue_client()

    # Reset the module-level flag between tests (it's a global).
    import worker.__main__ as wmain
    wmain._sigterm_received = False

    # Patch transcode_variant_by_id so it "starts" then the SIGTERM flag is flipped
    # mid-flight, mimicking the signal handler firing. The transcoder returns False
    # (ffmpeg was killed) — same as what cancel_active_ffmpeg causes in prod.
    async def _fake_transcode(session_factory, asset_dir, target_id):
        wmain._sigterm_received = True
        return False

    settings = MagicMock()
    settings.asset_storage_path = tmp_path / "assets"
    settings.asset_storage_path.mkdir()
    settings.azure_storage_connection_string = "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=y;EndpointSuffix=core.windows.net"

    with patch("worker.__main__.get_session_factory", return_value=factory), \
         patch("worker.transcoder.transcode_variant_by_id", new=AsyncMock(side_effect=_fake_transcode)), \
         patch("worker.transcoder.capture_stream_by_id", new=AsyncMock(return_value=False)), \
         patch("azure.storage.queue.QueueClient.from_connection_string", return_value=queue_client):
        queue_client.receive_message.return_value = queue_msg
        try:
            await wmain._queue_mode(settings)
        finally:
            wmain._sigterm_received = False  # don't leak into other tests

    # Job should be FAILED with the exact user-facing message
    async with factory() as db:
        job_row = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
        variant_row = (await db.execute(
            select(AssetVariant).where(AssetVariant.id == variant_id)
        )).scalar_one()

    assert job_row.status == JobStatus.FAILED
    assert job_row.error_message == "Transcode exceeded the 2 hour time limit."
    assert variant_row.status == VariantStatus.FAILED
    assert variant_row.error_message == "Transcode exceeded the 2 hour time limit."

    # Queue message must be deleted — no retry budget consumed
    assert queue_client.delete_message.called, "SIGTERM path must delete the queue message"


@_requires_azure_queue
@pytest.mark.asyncio
async def test_lease_lost_path_is_silent(db_engine, tmp_path):
    """On lease loss (heartbeat update_message fails), worker exits without any writes.

    The replacement worker (which now owns the re-delivered message) must not be
    stomped on — we MUST NOT write to Job/AssetVariant or delete the queue message.
    """
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as db:
        job_id, variant_id = await _seed_asset_variant_job(db)

    queue_msg = _make_queue_msg(str(job_id))
    queue_client = _make_queue_client()
    # First update_message call raises — lease immediately lost
    queue_client.update_message.side_effect = RuntimeError("MessageNotFound — lease gone")

    import worker.__main__ as wmain
    wmain._sigterm_received = False

    # Give transcode enough time for the heartbeat to run its first iteration
    # (which fires update_message immediately on start per the renew-first change)
    # and for lease_actually_lost to propagate before we return.
    async def _slow_fake_transcode(session_factory, asset_dir, target_id):
        await asyncio.sleep(0.2)  # heartbeat fires update_message, fails, kills us
        return False

    settings = MagicMock()
    settings.asset_storage_path = tmp_path / "assets"
    settings.asset_storage_path.mkdir()
    settings.azure_storage_connection_string = "DefaultEndpointsProtocol=https;AccountName=x;AccountKey=y;EndpointSuffix=core.windows.net"

    with patch("worker.__main__.get_session_factory", return_value=factory), \
         patch("worker.transcoder.transcode_variant_by_id", new=AsyncMock(side_effect=_slow_fake_transcode)), \
         patch("worker.transcoder.capture_stream_by_id", new=AsyncMock(return_value=False)), \
         patch("worker.transcoder.cancel_active_ffmpeg"), \
         patch("azure.storage.queue.QueueClient.from_connection_string", return_value=queue_client):
        queue_client.receive_message.return_value = queue_msg
        await wmain._queue_mode(settings)

    # Verify claim_job ran (job.status moved to PROCESSING, retry_count bumped to 1)
    # but the finalize path wrote NOTHING further — variant row is pristine PENDING.
    async with factory() as db:
        variant_row = (await db.execute(
            select(AssetVariant).where(AssetVariant.id == variant_id)
        )).scalar_one()

    # Silent path: variant must NOT have been touched with cancel/failed/done.
    assert variant_row.status == VariantStatus.PENDING, (
        f"lease-lost path must not touch variant; got {variant_row.status}"
    )
    # Queue message must NOT be deleted — the replacement worker needs to see it.
    assert not queue_client.delete_message.called, (
        "lease-lost path must not delete the queue message; replacement worker owns it"
    )


# ── Listen-mode resilience ──
#
# Regression coverage for the v1.37.19 incident: a transient DB error inside
# ``process_captures`` killed the worker process.  With no compose ``restart``
# policy, the container stayed dead and every queued capture/transcode sat
# PENDING forever.  ``_listen_mode_robust`` now wraps each iteration in
# try/except so a single bad iteration logs and backs off instead of exiting.


@pytest.mark.asyncio
async def test_listen_mode_survives_transient_db_error(monkeypatch, caplog):
    """A DBAPIError from process_captures must NOT exit the listen loop."""
    import logging

    import worker.__main__ as wmain

    # Build a minimal settings stub — _listen_mode_robust only reads three
    # fields from it, and we monkeypatch everything else away.
    settings = MagicMock()
    settings.database_url = "postgresql+asyncpg://stub/stub"
    settings.asset_storage_path = "/tmp/agora-test-assets"
    settings.poll_interval = 0  # immediately fall through the wait

    # Track how many times each work function is called so we can prove the
    # loop kept iterating after the failure.
    capture_calls = 0
    pending_calls = 0

    async def _failing_captures(_factory, _dir):
        nonlocal capture_calls
        capture_calls += 1
        if capture_calls == 2:
            # Iteration 1 = startup pass (must succeed for crash recovery).
            # Iteration 2 = first loop iteration — inject the failure.
            from sqlalchemy.exc import InterfaceError
            raise InterfaceError("stub", {}, Exception("connection is closed"))
        return 0

    async def _ok_pending(_factory, _dir):
        nonlocal pending_calls
        pending_calls += 1
        # After we've exercised the loop a few times, request shutdown so
        # the test exits deterministically.
        if pending_calls >= 3:
            wmain_shutdown_event.set()
        return 0

    # Capture the shutdown Event the function creates so we can flip it from
    # _ok_pending without reaching into _listen_mode_robust internals.
    wmain_shutdown_event = asyncio.Event()
    real_event_cls = asyncio.Event

    def _patched_event():
        # Return our controllable event the FIRST time (the shutdown event),
        # then fall back to a normal Event for work_available.
        if not _patched_event.taken:
            _patched_event.taken = True
            return wmain_shutdown_event
        return real_event_cls()
    _patched_event.taken = False

    fake_listener_conn = AsyncMock()
    fake_listener_conn.add_listener = AsyncMock()
    fake_listener_conn.remove_listener = AsyncMock()
    fake_listener_conn.close = AsyncMock()

    async def _fake_asyncpg_connect(_url):
        return fake_listener_conn

    fake_asyncpg = MagicMock()
    fake_asyncpg.connect = _fake_asyncpg_connect

    # Windows test runners don't support loop.add_signal_handler — patch it
    # to a no-op so the test isn't platform-gated (production worker only
    # runs on Linux).
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "add_signal_handler", lambda *_a, **_k: None)

    with patch.dict("sys.modules", {"asyncpg": fake_asyncpg}), \
         patch.object(wmain, "get_session_factory", return_value=lambda: None), \
         patch.object(wmain, "recover_interrupted", new=AsyncMock()), \
         patch.object(wmain, "process_captures", new=_failing_captures), \
         patch.object(wmain, "process_pending", new=_ok_pending), \
         patch.object(asyncio, "Event", _patched_event):
        # Bound the test so a regression (loop dies) doesn't hang forever.
        with caplog.at_level(logging.ERROR, logger="agora"):
            await asyncio.wait_for(wmain._listen_mode_robust(settings), timeout=10.0)

    # Loop body must have run at least three times after the startup pass —
    # iteration 2 raised, but the wrapper kept us going into iteration 3+
    # which finally tripped the shutdown Event.
    assert capture_calls >= 3, (
        f"expected ≥3 process_captures calls (startup + ≥2 loop iterations); "
        f"got {capture_calls}.  The listen loop likely died on the injected "
        f"InterfaceError — regression of the v1.37.19 incident."
    )
    assert pending_calls >= 3
    # And we must have logged the failure (defence-in-depth: silent retries
    # would mask real outages).
    assert any(
        "iteration failed" in rec.getMessage().lower()
        for rec in caplog.records
    ), "transient failure was not logged"
