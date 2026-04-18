"""Job queue service — creates Job rows and Azure queue messages.

This is the primary API for both the CMS (producer) and the worker (consumer).
A job is a single unit of work whose UUID is the queue message body.

Enqueue uses a **transactional outbox**: the producer INSERTs a ``Job`` row
and a ``JobOutbox`` row in the same DB transaction.  A separate drainer task
(``drain_outbox``, scheduled by the CMS) reads the outbox, calls
``queue.send_message``, and DELETEs the outbox row.  This guarantees
at-least-once enqueue regardless of when the CMS crashes or the queue API
fails — the row survives, the drainer retries.

Environment:
  - ``AGORA_CMS_AZURE_STORAGE_CONNECTION_STRING`` — if set, queue messages are
    sent to the ``transcode-jobs`` Azure Storage Queue.  If unset (docker-compose
    tests), a PostgreSQL NOTIFY is used as a fallback wake-up signal only;
    the Job row is still the source of truth.
"""

import asyncio
import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.job import (
    Job,
    JobOutbox,
    JobStatus,
    JobType,
    MAX_JOB_RETRIES,
    MAX_OUTBOX_ATTEMPTS,
)

logger = logging.getLogger("agora.jobs")

QUEUE_NAME = "transcode-jobs"

# ── Outbox drainer config ──
# Drainer pulls at most this many rows per cycle.  Keeps each cycle bounded
# so a huge backlog can't starve the rest of the scheduler loop.
OUTBOX_BATCH_SIZE = int(os.environ.get("AGORA_OUTBOX_BATCH_SIZE", "100"))

# Cap exponential backoff per row at this many seconds.  A persistently
# failing row will be retried at most once per minute.
OUTBOX_MAX_BACKOFF_SECONDS = 60

# PostgreSQL NOTIFY channels.  ``transcode_outbox`` wakes the drainer when
# a producer commits a new outbox row.  ``transcode_jobs`` is the existing
# compose-mode worker wake-up; the drainer fires it after each successful
# send so LISTEN-mode workers still see jobs immediately.
OUTBOX_NOTIFY_CHANNEL = "transcode_outbox"
JOB_NOTIFY_CHANNEL = "transcode_jobs"


def _as_utc(dt: datetime) -> datetime:
    """Coerce a possibly-naive datetime to timezone-aware UTC.

    SQLite (used in some tests) drops timezone info on round-trip;
    PostgreSQL preserves it.  Treat naive values as UTC since that's
    what the producers always write.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _get_queue_client():
    """Return a QueueClient if Azure is configured, else None."""
    conn_str = os.environ.get("AGORA_CMS_AZURE_STORAGE_CONNECTION_STRING")
    if not conn_str:
        return None
    try:
        from azure.storage.queue import QueueClient
        return QueueClient.from_connection_string(conn_str, QUEUE_NAME)
    except Exception:
        logger.warning("Failed to build QueueClient", exc_info=True)
        return None


async def _notify(db: AsyncSession, channel: str) -> None:
    """Fire PostgreSQL NOTIFY on a channel (no-op for non-postgres dialects).

    Best-effort: rolls back the inner NOTIFY transaction on failure but
    never raises — the outbox row is the durable signal.
    """
    dialect = db.bind.dialect.name if db.bind else ""
    if dialect != "postgresql":
        return
    try:
        await db.execute(text(f"NOTIFY {channel}"))
        await db.commit()
    except Exception:
        await db.rollback()
        logger.debug("NOTIFY %s failed (non-critical)", channel, exc_info=True)


async def enqueue_job(
    db: AsyncSession,
    job_type: JobType,
    target_id: uuid.UUID,
) -> uuid.UUID:
    """Create a Job row and an outbox row in a single transaction.

    Returns the new job's UUID.  The actual queue message is sent
    asynchronously by ``drain_outbox`` — typically within one drainer
    tick (sub-second when NOTIFY-driven, ≤5s in the polling fallback).
    """
    job = Job(type=job_type, target_id=target_id, status=JobStatus.PENDING)
    db.add(job)
    await db.flush()  # populate job.id without committing
    db.add(JobOutbox(job_id=job.id))
    await db.commit()
    await db.refresh(job)
    await _notify(db, OUTBOX_NOTIFY_CHANNEL)
    logger.debug("Enqueued job %s type=%s target=%s", job.id, job_type.value, target_id)
    return job.id


async def enqueue_jobs(
    db: AsyncSession,
    specs: Iterable[tuple[JobType, uuid.UUID]],
) -> list[uuid.UUID]:
    """Bulk-create Job + outbox rows in a single transaction."""
    specs = list(specs)
    if not specs:
        return []
    jobs = [Job(type=t, target_id=tid, status=JobStatus.PENDING) for t, tid in specs]
    db.add_all(jobs)
    await db.flush()
    db.add_all([JobOutbox(job_id=j.id) for j in jobs])
    await db.commit()
    for j in jobs:
        await db.refresh(j)
    await _notify(db, OUTBOX_NOTIFY_CHANNEL)
    logger.info("Enqueued %d job(s) via outbox", len(jobs))
    return [j.id for j in jobs]


async def _send_queue_message(job_id: uuid.UUID) -> None:
    """Send a single queue message (body = job UUID string).

    Raises on send failure so the drainer can record the error and back off.
    Returns silently if no queue client is configured (compose mode).
    """
    queue = _get_queue_client()
    if queue is None:
        return
    # azure-storage-queue is a sync SDK; run in a thread so we don't block
    # the asyncio loop on slow network calls.
    await asyncio.to_thread(queue.send_message, str(job_id))


async def resend_queue_message(job_id: uuid.UUID) -> None:
    """Re-send a queue message for an existing Job row (legacy sweep helper).

    Kept for backward compatibility with callers that still want a fire-and-
    forget resend.  Swallows errors.
    """
    try:
        await _send_queue_message(job_id)
    except Exception:
        logger.warning("Failed to resend queue message for job %s", job_id, exc_info=True)


async def claim_job(db: AsyncSession, job_id: uuid.UUID) -> Job | None:
    """Claim a job for processing.

    Queue is authority: if we received the message, we own the job.  This
    function just bumps retry_count and stamps status=PROCESSING.  Returns
    the Job row, or None if the row has been deleted (stale message →
    caller should just delete the queue message).

    If retry_count would exceed MAX_JOB_RETRIES, marks the job FAILED and
    returns the row so the caller can short-circuit.
    """
    from sqlalchemy import select

    async with db.begin():
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            return None

        if job.status == JobStatus.DONE:
            # Duplicate message for completed job — caller should delete & skip
            return job

        job.retry_count += 1
        if job.retry_count > MAX_JOB_RETRIES:
            job.status = JobStatus.FAILED
            job.error_message = f"exceeded retry limit ({MAX_JOB_RETRIES})"
            job.completed_at = datetime.now(timezone.utc)
            logger.error(
                "Job %s type=%s target=%s exceeded retry limit; marking FAILED",
                job.id, job.type.value, job.target_id,
            )
        else:
            job.status = JobStatus.PROCESSING
            job.error_message = ""
    return job


async def mark_done(db: AsyncSession, job_id: uuid.UUID) -> None:
    """Mark a job DONE."""
    from sqlalchemy import select

    async with db.begin():
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            return
        job.status = JobStatus.DONE
        job.error_message = ""
        job.completed_at = datetime.now(timezone.utc)


async def mark_failed(db: AsyncSession, job_id: uuid.UUID, error: str) -> None:
    """Mark a job FAILED with an error message (terminal — no retry)."""
    from sqlalchemy import select

    async with db.begin():
        result = await db.execute(select(Job).where(Job.id == job_id))
        job = result.scalar_one_or_none()
        if job is None:
            return
        job.status = JobStatus.FAILED
        job.error_message = error[:2000]
        job.completed_at = datetime.now(timezone.utc)


async def drain_outbox(db: AsyncSession) -> int:
    """Drain pending JobOutbox rows by sending their queue messages.

    Process up to ``OUTBOX_BATCH_SIZE`` rows ordered by ``created_at`` ASC.
    For each row:
      - Skip if its exponential-backoff window hasn't elapsed.
      - Skip (leave in place for alerting) if it has hit ``MAX_OUTBOX_ATTEMPTS``.
      - Call ``_send_queue_message``.  On success, DELETE the outbox row.
        On failure, increment ``attempts`` and record the error.

    In compose mode (no Azure queue client), the send is a no-op and the
    row is deleted — the existing ``transcode_jobs`` NOTIFY (fired below)
    is the wake-up signal for LISTEN-mode workers.

    Returns the number of rows successfully drained (sent + deleted).
    Safe to call concurrently across replicas — on Postgres we use
    ``SELECT … FOR UPDATE SKIP LOCKED`` so two drainers grab disjoint
    batches.  As a backstop, the worker dedupes via ``claim_job``.
    """
    now = datetime.now(timezone.utc)
    stmt = (
        select(JobOutbox)
        .order_by(JobOutbox.created_at.asc())
        .limit(OUTBOX_BATCH_SIZE)
    )
    # On Postgres, lock the rows we're about to drain so a second CMS
    # replica's drainer skips them and grabs the next batch instead of
    # racing on the same rows. SQLite (used in tests) doesn't support
    # SELECT … FOR UPDATE, so we fall back to a plain select there.
    bind = db.get_bind() if hasattr(db, "get_bind") else None
    dialect_name = getattr(getattr(bind, "dialect", None), "name", "")
    if dialect_name == "postgresql":
        stmt = stmt.with_for_update(skip_locked=True)
    result = await db.execute(stmt)
    rows = result.scalars().all()
    if not rows:
        return 0

    sent = 0
    notify_workers = False
    for row in rows:
        if row.attempts >= MAX_OUTBOX_ATTEMPTS:
            # Cap reached — leave the row for ops to investigate. Logged
            # once per drain cycle below so we don't spam the log.
            continue

        if row.last_attempt_at is not None:
            backoff = min(2 ** row.attempts, OUTBOX_MAX_BACKOFF_SECONDS)
            next_attempt = _as_utc(row.last_attempt_at) + timedelta(seconds=backoff)
            if next_attempt > now:
                continue

        try:
            await _send_queue_message(row.job_id)
        except Exception as exc:
            row.attempts += 1
            row.last_attempt_at = now
            row.last_error = str(exc)[:2000]
            logger.warning(
                "Outbox send failed for job %s (attempt %d): %s",
                row.job_id, row.attempts, exc,
            )
            continue

        # Success (real send or compose-mode no-op): drop the row.
        await db.delete(row)
        sent += 1
        notify_workers = True

    await db.commit()

    # Surface stuck rows and oldest-age signal once per cycle.
    stuck_rows = [r for r in rows if r.attempts >= MAX_OUTBOX_ATTEMPTS]
    if stuck_rows:
        oldest_stuck = min(_as_utc(r.created_at) for r in stuck_rows)
        age = (now - oldest_stuck).total_seconds()
        logger.error(
            "Outbox: %d row(s) at MAX_OUTBOX_ATTEMPTS=%d (oldest %.0fs old) — investigate",
            len(stuck_rows), MAX_OUTBOX_ATTEMPTS, age,
        )

    if sent:
        logger.info("Outbox drained %d row(s)", sent)
        if notify_workers:
            # Wake compose-mode LISTEN workers immediately.
            await _notify(db, JOB_NOTIFY_CHANNEL)
    return sent


async def outbox_oldest_age_seconds(db: AsyncSession) -> float | None:
    """Return age in seconds of the oldest pending outbox row, or None if empty.

    Used by health checks / observability to detect a stalled drainer.
    Suggested thresholds: warn > 60s, error > 300s.
    """
    result = await db.execute(
        select(JobOutbox.created_at).order_by(JobOutbox.created_at.asc()).limit(1)
    )
    oldest = result.scalar_one_or_none()
    if oldest is None:
        return None
    return (datetime.now(timezone.utc) - _as_utc(oldest)).total_seconds()
