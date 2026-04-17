"""Transcode worker entrypoint — run with ``python -m worker``.

Two modes controlled by AGORA_WORKER_MODE (or AGORA_CMS_WORKER_MODE):

  listen  (default, Docker Compose)
    Connects to PostgreSQL via raw asyncpg, executes LISTEN transcode_jobs,
    and processes all PENDING variants whenever a notification arrives.
    Falls back to polling every 60 s for resilience.

  queue  (Azure Container Apps Job)
    Triggered by an Azure Storage Queue message. Processes all PENDING
    variants, then exits (container scales to zero).
"""

import asyncio
import logging
import signal
import sys

from worker.config import WorkerSettings
from worker.transcoder import process_captures, process_pending, recover_interrupted
from shared.models import AssetVariant, VariantStatus

from shared.config import SharedSettings
from shared.database import init_db, get_session_factory, dispose_db
from shared.services.storage import (
    AzureStorageBackend,
    LocalStorageBackend,
    init_storage,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger("agora.worker")


async def _listen_mode(settings: WorkerSettings) -> None:
    """LISTEN/NOTIFY mode — long-running loop for Docker Compose."""
    import asyncpg

    # Extract raw connection params from the SQLAlchemy URL
    # Format: postgresql+asyncpg://user:pass@host:port/dbname
    url = settings.database_url
    raw_url = url.replace("postgresql+asyncpg://", "postgresql://", 1)

    asset_dir = settings.asset_storage_path
    session_factory = get_session_factory()
    poll_interval = settings.poll_interval

    # Crash recovery
    await recover_interrupted(session_factory)

    # Process any already-pending work before entering the listen loop
    captures = await process_captures(session_factory, asset_dir)
    if captures:
        logger.info("Captured %d stream(s) on startup", captures)
    count = await process_pending(session_factory, asset_dir)
    if count:
        logger.info("Processed %d pending variant(s) on startup", count)

    conn = await asyncpg.connect(raw_url)
    logger.info("Connected to PostgreSQL, listening for transcode_jobs (poll fallback: %ds)", poll_interval)

    shutdown = asyncio.Event()

    def _on_signal():
        logger.info("Shutdown signal received")
        shutdown.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    await conn.add_listener("transcode_jobs", lambda *_: None)

    try:
        while not shutdown.is_set():
            # Wait for a notification or timeout
            try:
                notification = await asyncio.wait_for(
                    conn.fetchrow("SELECT 1"),  # dummy query — notifications arrive via listener
                    timeout=0.1,
                )
            except asyncio.TimeoutError:
                pass

            # Check if we got a notification via the listener callback
            # asyncpg delivers notifications on the connection; we poll briefly
            # then process any pending work
            try:
                await asyncio.wait_for(shutdown.wait(), timeout=poll_interval)
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass  # poll timeout — check for work

            captures = await process_captures(session_factory, asset_dir)
            if captures:
                logger.info("Captured %d stream(s)", captures)
            count = await process_pending(session_factory, asset_dir)
            if count:
                logger.info("Processed %d variant(s)", count)
    finally:
        await conn.close()


async def _listen_mode_robust(settings: WorkerSettings) -> None:
    """Robust LISTEN/NOTIFY mode with proper notification handling."""
    import asyncpg

    url = settings.database_url
    raw_url = url.replace("postgresql+asyncpg://", "postgresql://", 1)

    asset_dir = settings.asset_storage_path
    session_factory = get_session_factory()
    poll_interval = settings.poll_interval

    # Crash recovery
    await recover_interrupted(session_factory)

    # Process any already-pending work
    count = await process_pending(session_factory, asset_dir)
    if count:
        logger.info("Processed %d pending variant(s) on startup", count)

    shutdown = asyncio.Event()
    work_available = asyncio.Event()

    def _on_signal():
        logger.info("Shutdown signal received")
        shutdown.set()
        work_available.set()  # unblock wait

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, _on_signal)

    def _on_notification(conn, pid, channel, payload):
        logger.debug("Received NOTIFY on %s", channel)
        work_available.set()

    conn = await asyncpg.connect(raw_url)
    await conn.add_listener("transcode_jobs", _on_notification)
    logger.info("Listening for transcode_jobs notifications (poll fallback: %ds)", poll_interval)

    try:
        while not shutdown.is_set():
            work_available.clear()

            # Wait for notification or poll timeout
            try:
                await asyncio.wait_for(work_available.wait(), timeout=poll_interval)
            except asyncio.TimeoutError:
                pass  # poll fallback

            if shutdown.is_set():
                break

            captures = await process_captures(session_factory, asset_dir)
            if captures:
                logger.info("Captured %d stream(s)", captures)
            count = await process_pending(session_factory, asset_dir)
            if count:
                logger.info("Processed %d variant(s)", count)
    finally:
        await conn.remove_listener("transcode_jobs", _on_notification)
        await conn.close()


async def _drain_queue(settings: WorkerSettings) -> int:
    """Dequeue and delete all messages from the Azure Storage Queue.

    Returns the number of messages drained.  This prevents KEDA from
    re-triggering the job for messages that have already been acted on.
    """
    conn_str = settings.azure_storage_connection_string
    if not conn_str:
        return 0

    try:
        from azure.storage.queue import QueueClient
        queue = QueueClient.from_connection_string(conn_str, "transcode-jobs")
        drained = 0
        while True:
            msgs = queue.receive_messages(messages_per_page=32, visibility_timeout=30)
            batch = list(msgs)
            if not batch:
                break
            for msg in batch:
                queue.delete_message(msg)
                drained += 1
        if drained:
            logger.info("Drained %d message(s) from transcode-jobs queue", drained)
        return drained
    except Exception:
        logger.warning("Failed to drain transcode-jobs queue", exc_info=True)
        return 0


async def _queue_mode(settings: WorkerSettings) -> None:
    """Queue mode — process one job per worker invocation, then exit.

    Receives a single message from the ``transcode-jobs`` Azure Storage
    Queue with a 30-second visibility timeout.  A background heartbeat
    task extends the lease every 15 seconds while work is in progress.
    The queue is the source of authority: owning the message means
    owning the job, irrespective of DB state.

    Flow:
      1. Receive one message (visibility_timeout=30).
      2. Parse job UUID from body, load Job row.
      3. Claim (bump retry_count; if > MAX_RETRIES mark FAILED & delete).
      4. Start 15s heartbeat (update_message) to hold the lease.
      5. Dispatch on job.type to variant transcode or stream capture.
      6. On success: mark job DONE, stop heartbeat, delete message.
      7. On failure / crash: stop heartbeat, do NOT delete message — it
         becomes visible again after the visibility timeout and another
         worker invocation retries.
    """
    import uuid as _uuid

    from shared.models.job import Job, JobStatus, JobType
    from shared.services.jobs import claim_job, mark_done, mark_failed, QUEUE_NAME
    from worker.transcoder import (
        capture_stream_by_id,
        recover_interrupted,
        transcode_variant_by_id,
    )
    from sqlalchemy import select

    session_factory = get_session_factory()
    asset_dir = settings.asset_storage_path

    # Crash recovery (cheap idempotent PROCESSING→PENDING reset for interrupted work)
    await recover_interrupted(session_factory)

    conn_str = settings.azure_storage_connection_string
    if not conn_str:
        logger.warning("Queue mode requested but no Azure connection string — exiting")
        return

    from azure.storage.queue import QueueClient
    queue = QueueClient.from_connection_string(conn_str, QUEUE_NAME)

    VISIBILITY_TIMEOUT = 30
    HEARTBEAT_INTERVAL = 15

    msgs = list(queue.receive_messages(messages_per_page=1, visibility_timeout=VISIBILITY_TIMEOUT))
    if not msgs:
        logger.info("Queue mode: no messages, exiting")
        return

    msg = msgs[0]
    body = msg.content.strip() if isinstance(msg.content, str) else str(msg.content).strip()

    # Parse UUID.  Legacy messages (body = "transcode") have no job id — drop them.
    try:
        job_id = _uuid.UUID(body)
    except (ValueError, AttributeError):
        logger.warning("Received non-UUID queue message %r — deleting", body)
        try:
            queue.delete_message(msg)
        except Exception:
            logger.exception("Failed to delete malformed message")
        return

    # Claim the job (bumps retry_count, flips to PROCESSING or FAILED-if-poison).
    async with session_factory() as db:
        job = await claim_job(db, job_id)

    if job is None:
        logger.warning("Job %s not found in DB — deleting stale message", job_id)
        try:
            queue.delete_message(msg)
        except Exception:
            logger.exception("Failed to delete stale message")
        return

    if job.status == JobStatus.DONE:
        logger.info("Job %s already DONE — deleting duplicate message", job_id)
        try:
            queue.delete_message(msg)
        except Exception:
            logger.exception("Failed to delete duplicate message")
        return

    if job.status == JobStatus.FAILED:
        # claim_job flipped us to FAILED because retry_count exceeded MAX.
        logger.error("Job %s exhausted retries — deleting poison message", job_id)
        try:
            queue.delete_message(msg)
        except Exception:
            logger.exception("Failed to delete poison message")
        return

    # ── Heartbeat: refresh the queue lease while work is in progress ──
    lease_lost = asyncio.Event()
    current_popreceipt = msg.pop_receipt

    async def _heartbeat():
        nonlocal current_popreceipt
        while not lease_lost.is_set():
            try:
                await asyncio.sleep(HEARTBEAT_INTERVAL)
                if lease_lost.is_set():
                    return
                updated = queue.update_message(
                    msg,
                    pop_receipt=current_popreceipt,
                    visibility_timeout=VISIBILITY_TIMEOUT,
                )
                # update_message returns an object with a fresh pop_receipt
                new_pr = getattr(updated, "pop_receipt", None)
                if new_pr:
                    current_popreceipt = new_pr
                logger.debug("Heartbeat refreshed lease for job %s", job_id)
            except Exception:
                logger.warning("Heartbeat failed for job %s — aborting", job_id, exc_info=True)
                lease_lost.set()
                return

    hb_task = asyncio.create_task(_heartbeat())

    success = False
    error_message: str | None = None
    try:
        logger.info(
            "Processing job %s type=%s target=%s (attempt %d)",
            job.id, job.type.value, job.target_id, job.retry_count,
        )
        if job.type == JobType.VARIANT_TRANSCODE:
            success = await transcode_variant_by_id(session_factory, asset_dir, job.target_id)
        elif job.type == JobType.STREAM_CAPTURE:
            success = await capture_stream_by_id(session_factory, asset_dir, job.target_id)
        else:
            error_message = f"Unknown job type: {job.type}"
            logger.error(error_message)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        error_message = f"{type(e).__name__}: {e}"
        logger.exception("Job %s raised an exception", job_id)
        success = False

    # Stop the heartbeat before doing any final DB/queue work.
    lease_lost.set()
    hb_task.cancel()
    try:
        await hb_task
    except (asyncio.CancelledError, Exception):
        pass

    # ── Finalize ──
    if success:
        async with session_factory() as db:
            await mark_done(db, job_id)
        try:
            queue.delete_message(msg, pop_receipt=current_popreceipt)
            logger.info("Job %s complete", job_id)
        except Exception:
            # If the delete fails because the lease expired, the message will
            # come back; mark_done above ensures the retry is a no-op.
            logger.warning("Job %s done but delete failed", job_id, exc_info=True)
    else:
        # Record the failure in the Job row, but do NOT delete the queue
        # message — let it re-deliver after visibility timeout for retry.
        # (claim_job has already bumped retry_count; if the next attempt
        # also hits MAX it will be marked FAILED on claim.)
        async with session_factory() as db:
            # Flip back to PENDING so the next attempt can flip to PROCESSING
            from sqlalchemy import update
            await db.execute(
                update(Job)
                .where(Job.id == job_id)
                .values(
                    status=JobStatus.PENDING,
                    error_message=(error_message or "unknown failure")[:2000],
                )
            )
            await db.commit()
        logger.info("Job %s failed — will retry after visibility timeout", job_id)


async def _wait_for_schema(max_retries: int = 30, delay: float = 2.0) -> None:
    """Block until the CMS has created/migrated the database schema."""
    from sqlalchemy import text
    session_factory = get_session_factory()
    for attempt in range(1, max_retries + 1):
        try:
            async with session_factory() as db:
                # Check base tables + enums + the jobs table (added in queue-rework)
                await db.execute(text("SELECT 1 FROM asset_variants LIMIT 0"))
                await db.execute(text(
                    "SELECT 1 FROM pg_enum WHERE enumlabel = 'SAVED_STREAM' "
                    "AND enumtypid = 'assettype'::regtype"
                ))
                await db.execute(text("SELECT 1 FROM jobs LIMIT 0"))
                return
        except Exception:
            if attempt == max_retries:
                raise RuntimeError(
                    "Database schema not ready after %d attempts — "
                    "is the CMS container running?" % max_retries
                )
            logger.info("Waiting for database schema (attempt %d/%d)…", attempt, max_retries)
            await asyncio.sleep(delay)


async def main() -> None:
    settings = WorkerSettings()
    init_db(settings)

    # Wait for the CMS to create the database schema before proceeding
    await _wait_for_schema()

    # Initialize storage backend
    if settings.storage_backend == "azure":
        if not settings.azure_storage_connection_string:
            raise RuntimeError(
                "AGORA_CMS_AZURE_STORAGE_CONNECTION_STRING is required "
                "when storage_backend is 'azure'"
            )
        backend = AzureStorageBackend(
            base_path=settings.asset_storage_path,
            connection_string=settings.azure_storage_connection_string,
            account_name=settings.azure_storage_account_name,
            account_key=settings.azure_storage_account_key,
            sas_expiry_hours=settings.azure_sas_expiry_hours,
        )
    else:
        backend = LocalStorageBackend(base_path=settings.asset_storage_path)
    init_storage(backend)

    mode = settings.worker_mode
    logger.info("Transcode worker starting (mode=%s)", mode)

    try:
        if mode == "queue":
            await _queue_mode(settings)
        else:
            await _listen_mode_robust(settings)
    finally:
        if hasattr(backend, "close"):
            await backend.close()
        await dispose_db()


if __name__ == "__main__":
    asyncio.run(main())
