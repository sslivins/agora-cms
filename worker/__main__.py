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
from worker.transcoder import process_pending, recover_interrupted
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
    """Queue mode — process all pending variants, then exit."""
    session_factory = get_session_factory()
    asset_dir = settings.asset_storage_path

    # Crash recovery
    await recover_interrupted(session_factory)

    count = await process_pending(session_factory, asset_dir)
    logger.info("Queue mode: processed %d variant(s), exiting", count)

    # Only drain the queue when no PENDING variants remain.  If we
    # deferred work (e.g. stream sibling-check), leave the messages so
    # KEDA keeps firing workers that can pick up the work later.
    async with session_factory() as db:
        from sqlalchemy import select as sa_select
        remaining = await db.execute(
            sa_select(AssetVariant.id)
            .where(AssetVariant.status == VariantStatus.PENDING)
            .limit(1)
        )
        if remaining.scalar_one_or_none() is None:
            await _drain_queue(settings)
        else:
            logger.info("PENDING variants remain — leaving queue messages for KEDA")


async def _wait_for_schema(max_retries: int = 30, delay: float = 2.0) -> None:
    """Block until the CMS has created/migrated the database schema."""
    from sqlalchemy import text
    session_factory = get_session_factory()
    for attempt in range(1, max_retries + 1):
        try:
            async with session_factory() as db:
                # Check both base table and latest migration columns/enums
                await db.execute(text("SELECT 1 FROM asset_variants LIMIT 0"))
                await db.execute(text(
                    "SELECT 1 FROM pg_enum WHERE enumlabel = 'SAVED_STREAM' "
                    "AND enumtypid = 'assettype'::regtype"
                ))
                await db.execute(text("SELECT retry_count FROM asset_variants LIMIT 0"))
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
