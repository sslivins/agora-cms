"""Tests for the transactional-outbox enqueue path.

Producer paths (``enqueue_job`` / ``enqueue_jobs``) write a ``Job`` row and
a ``JobOutbox`` row in the same DB transaction; ``drain_outbox`` is the only
caller of ``_send_queue_message``.

Coverage:
  - enqueue_job INSERTs both rows; nothing is sent inline.
  - drain_outbox sends and deletes on success.
  - drain_outbox increments attempts + records error on send failure.
  - drain_outbox respects exponential backoff between attempts.
  - drain_outbox stops trying after MAX_OUTBOX_ATTEMPTS.
  - drain_outbox is a no-op (and deletes rows) when no queue client is
    configured (compose mode).
  - Cascade-delete removes the outbox row when the Job is deleted.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from shared.models.job import Job, JobOutbox, JobStatus, JobType, MAX_OUTBOX_ATTEMPTS
from shared.services import jobs as jobs_svc


# ── enqueue_job / enqueue_jobs ──

@pytest.mark.asyncio
async def test_enqueue_job_writes_job_and_outbox_rows(db_session):
    """enqueue_job creates a Job row + an outbox row in the same tx."""
    target = uuid.uuid4()

    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()) as send:
        job_id = await jobs_svc.enqueue_job(
            db_session, JobType.VARIANT_TRANSCODE, target,
        )

    # Inline send must NOT happen — drainer owns that now.
    send.assert_not_called()

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    assert job.status == JobStatus.PENDING
    assert job.type == JobType.VARIANT_TRANSCODE
    assert job.target_id == target

    outbox = (
        await db_session.execute(select(JobOutbox).where(JobOutbox.job_id == job_id))
    ).scalar_one()
    assert outbox.attempts == 0
    assert outbox.last_attempt_at is None
    assert outbox.last_error == ""


@pytest.mark.asyncio
async def test_enqueue_jobs_bulk_writes_paired_rows(db_session):
    targets = [(JobType.VARIANT_TRANSCODE, uuid.uuid4()) for _ in range(3)]

    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()) as send:
        ids = await jobs_svc.enqueue_jobs(db_session, targets)

    send.assert_not_called()
    assert len(ids) == 3

    outbox_ids = (
        await db_session.execute(
            select(JobOutbox.job_id).where(JobOutbox.job_id.in_(ids))
        )
    ).scalars().all()
    assert sorted(outbox_ids) == sorted(ids)


# ── drain_outbox happy path ──

@pytest.mark.asyncio
async def test_drain_outbox_sends_and_deletes_on_success(db_session):
    target = uuid.uuid4()
    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()):
        job_id = await jobs_svc.enqueue_job(
            db_session, JobType.VARIANT_TRANSCODE, target,
        )

    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()) as send:
        sent = await jobs_svc.drain_outbox(db_session)

    assert sent == 1
    send.assert_awaited_once_with(job_id)

    remaining = (
        await db_session.execute(select(JobOutbox).where(JobOutbox.job_id == job_id))
    ).scalar_one_or_none()
    assert remaining is None, "outbox row should be deleted after successful send"


@pytest.mark.asyncio
async def test_drain_outbox_returns_zero_when_empty(db_session):
    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()) as send:
        sent = await jobs_svc.drain_outbox(db_session)
    assert sent == 0
    send.assert_not_called()


# ── drain_outbox failure path ──

@pytest.mark.asyncio
async def test_drain_outbox_records_error_and_keeps_row_on_failure(db_session):
    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()):
        job_id = await jobs_svc.enqueue_job(
            db_session, JobType.VARIANT_TRANSCODE, uuid.uuid4(),
        )

    fail = AsyncMock(side_effect=RuntimeError("queue exploded"))
    with patch.object(jobs_svc, "_send_queue_message", new=fail):
        sent = await jobs_svc.drain_outbox(db_session)

    assert sent == 0
    row = (
        await db_session.execute(select(JobOutbox).where(JobOutbox.job_id == job_id))
    ).scalar_one()
    assert row.attempts == 1
    assert row.last_attempt_at is not None
    assert "queue exploded" in row.last_error


@pytest.mark.asyncio
async def test_drain_outbox_skips_row_within_backoff_window(db_session):
    """A row that just failed should be skipped on the next drain."""
    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()):
        job_id = await jobs_svc.enqueue_job(
            db_session, JobType.VARIANT_TRANSCODE, uuid.uuid4(),
        )

    # Stamp a recent failure: attempts=2 → 4s backoff window.
    row = (
        await db_session.execute(select(JobOutbox).where(JobOutbox.job_id == job_id))
    ).scalar_one()
    row.attempts = 2
    row.last_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=1)
    row.last_error = "prev"
    await db_session.commit()

    send = AsyncMock()
    with patch.object(jobs_svc, "_send_queue_message", new=send):
        sent = await jobs_svc.drain_outbox(db_session)

    assert sent == 0
    send.assert_not_called(), "should respect backoff window"

    # Now move last_attempt_at past the window and confirm we retry.
    row = (
        await db_session.execute(select(JobOutbox).where(JobOutbox.job_id == job_id))
    ).scalar_one()
    row.last_attempt_at = datetime.now(timezone.utc) - timedelta(seconds=120)
    await db_session.commit()

    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()) as send2:
        sent = await jobs_svc.drain_outbox(db_session)

    assert sent == 1
    send2.assert_awaited_once_with(job_id)


@pytest.mark.asyncio
async def test_drain_outbox_caps_at_max_attempts(db_session):
    """Rows that hit MAX_OUTBOX_ATTEMPTS are left alone — never silently dropped."""
    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()):
        job_id = await jobs_svc.enqueue_job(
            db_session, JobType.VARIANT_TRANSCODE, uuid.uuid4(),
        )

    row = (
        await db_session.execute(select(JobOutbox).where(JobOutbox.job_id == job_id))
    ).scalar_one()
    row.attempts = MAX_OUTBOX_ATTEMPTS
    row.last_attempt_at = datetime.now(timezone.utc) - timedelta(hours=1)
    await db_session.commit()

    send = AsyncMock()
    with patch.object(jobs_svc, "_send_queue_message", new=send):
        sent = await jobs_svc.drain_outbox(db_session)

    assert sent == 0
    send.assert_not_called()

    # Row must still exist (so ops can investigate).
    still_there = (
        await db_session.execute(select(JobOutbox).where(JobOutbox.job_id == job_id))
    ).scalar_one_or_none()
    assert still_there is not None


# ── compose / no-queue-client mode ──

@pytest.mark.asyncio
async def test_drain_outbox_deletes_row_in_compose_mode(db_session, monkeypatch):
    """When no queue client is configured, drain still clears the outbox row."""
    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()):
        job_id = await jobs_svc.enqueue_job(
            db_session, JobType.VARIANT_TRANSCODE, uuid.uuid4(),
        )

    # Simulate compose mode: real _send_queue_message returns silently when
    # _get_queue_client() is None.
    monkeypatch.setattr(jobs_svc, "_get_queue_client", lambda: None)
    sent = await jobs_svc.drain_outbox(db_session)

    assert sent == 1
    remaining = (
        await db_session.execute(select(JobOutbox).where(JobOutbox.job_id == job_id))
    ).scalar_one_or_none()
    assert remaining is None


# ── observability helper ──

@pytest.mark.asyncio
async def test_outbox_oldest_age_seconds(db_session):
    assert await jobs_svc.outbox_oldest_age_seconds(db_session) is None

    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()):
        await jobs_svc.enqueue_job(
            db_session, JobType.VARIANT_TRANSCODE, uuid.uuid4(),
        )

    age = await jobs_svc.outbox_oldest_age_seconds(db_session)
    assert age is not None
    assert age >= 0


# ── cascade delete ──

@pytest.mark.asyncio
async def test_outbox_row_cascades_when_job_deleted(db_session):
    # SQLite doesn't enforce FK ON DELETE CASCADE without PRAGMA foreign_keys=ON,
    # which the test harness doesn't set.  PostgreSQL (CI) enforces it natively.
    if db_session.bind.dialect.name != "postgresql":
        pytest.skip("FK CASCADE only verified on PostgreSQL")

    with patch.object(jobs_svc, "_send_queue_message", new=AsyncMock()):
        job_id = await jobs_svc.enqueue_job(
            db_session, JobType.VARIANT_TRANSCODE, uuid.uuid4(),
        )

    job = (await db_session.execute(select(Job).where(Job.id == job_id))).scalar_one()
    await db_session.delete(job)
    await db_session.commit()

    remaining = (
        await db_session.execute(select(JobOutbox).where(JobOutbox.job_id == job_id))
    ).scalar_one_or_none()
    assert remaining is None
