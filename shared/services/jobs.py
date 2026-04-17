"""Job queue service — creates Job rows and Azure queue messages.

This is the primary API for both the CMS (producer) and the worker (consumer).
A job is a single unit of work whose UUID is the queue message body.

Environment:
  - ``AGORA_CMS_AZURE_STORAGE_CONNECTION_STRING`` — if set, queue messages are
    sent to the ``transcode-jobs`` Azure Storage Queue.  If unset (docker-compose
    tests), a PostgreSQL NOTIFY is used as a fallback wake-up signal only;
    the Job row is still the source of truth.
"""

import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.job import Job, JobStatus, JobType, MAX_JOB_RETRIES

logger = logging.getLogger("agora.jobs")

QUEUE_NAME = "transcode-jobs"


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


async def _notify_pg(db: AsyncSession) -> None:
    """Fire PostgreSQL NOTIFY so listen-mode workers wake up."""
    dialect = db.bind.dialect.name if db.bind else ""
    if dialect != "postgresql":
        return
    try:
        await db.execute(text("NOTIFY transcode_jobs"))
        await db.commit()
    except Exception:
        await db.rollback()
        logger.debug("NOTIFY transcode_jobs failed (non-critical)", exc_info=True)


async def enqueue_job(
    db: AsyncSession,
    job_type: JobType,
    target_id: uuid.UUID,
) -> uuid.UUID:
    """Create a Job row and send a queue message.

    Order: INSERT → commit → queue.send_message.  If the send fails (or CMS
    crashes between commit and send), the orphan sweep finds the PENDING row
    and re-sends.  The message body is just the job UUID.
    """
    job = Job(type=job_type, target_id=target_id, status=JobStatus.PENDING)
    db.add(job)
    await db.commit()
    await db.refresh(job)
    await _send_queue_message(job.id)
    await _notify_pg(db)
    logger.debug("Enqueued job %s type=%s target=%s", job.id, job_type.value, target_id)
    return job.id


async def enqueue_jobs(
    db: AsyncSession,
    specs: Iterable[tuple[JobType, uuid.UUID]],
) -> list[uuid.UUID]:
    """Bulk-create Job rows + queue messages.  Returns the list of job IDs."""
    specs = list(specs)
    if not specs:
        return []
    jobs = [Job(type=t, target_id=tid, status=JobStatus.PENDING) for t, tid in specs]
    db.add_all(jobs)
    await db.commit()
    for j in jobs:
        await db.refresh(j)
    for j in jobs:
        await _send_queue_message(j.id)
    await _notify_pg(db)
    logger.info("Enqueued %d job(s)", len(jobs))
    return [j.id for j in jobs]


async def _send_queue_message(job_id: uuid.UUID) -> None:
    """Send a single queue message (body = job UUID string)."""
    queue = _get_queue_client()
    if queue is None:
        return
    try:
        queue.send_message(str(job_id))
    except Exception:
        logger.warning("Failed to send queue message for job %s", job_id, exc_info=True)


async def resend_queue_message(job_id: uuid.UUID) -> None:
    """Re-send a queue message for an existing Job row (orphan sweep)."""
    await _send_queue_message(job_id)


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


async def sweep_orphans(db: AsyncSession, stale_seconds: int = 120) -> int:
    """Re-enqueue jobs that appear to be orphaned.

    An orphan is a PENDING job older than ``stale_seconds`` — either the CMS
    crashed between INSERT and queue.send_message, or the queue message was
    lost.  We just re-send the queue message; the Job row is left as-is.

    Returns the number of orphan jobs re-enqueued.
    """
    from datetime import timedelta
    from sqlalchemy import select

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
    result = await db.execute(
        select(Job.id).where(
            Job.status == JobStatus.PENDING,
            Job.created_at < cutoff,
        )
    )
    ids = [row[0] for row in result.all()]
    for jid in ids:
        await _send_queue_message(jid)
    if ids:
        logger.info("Orphan sweep re-enqueued %d job(s)", len(ids))
    return len(ids)
