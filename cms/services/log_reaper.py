"""Background blob-reaper for the ``log_requests`` outbox (Stage 3e of #345).

One asyncio loop per CMS replica.  Each tick:

1. Claim up to ``batch_size`` rows whose ``expires_at`` has passed
   (``SELECT ... FOR UPDATE SKIP LOCKED`` on Postgres so overlapping
   scans don't double-delete).
2. For each row with a ``blob_path``, call
   :func:`cms.services.log_blob.delete_log_blob`.  A blob that's
   already gone (backend returns ``False``) is counted as a benign
   miss — the row still transitions.
3. Update the row to ``status=expired``, ``blob_path=NULL`` so the
   UI sees the log bundle as cleaned up while the row lives on as
   a history tombstone.  A later, longer-tenured retention pass
   (not in this PR) can hard-delete rows whose ``expires_at`` is far
   enough in the past.

Idempotence:

* Already-expired rows are filtered out by the claim query so we
  never touch them twice.
* A blob-delete failure (network blip, transient Azure error) keeps
  the row as-is — the next tick retries.  The row stays "expired"
  eligible because its ``expires_at`` is unchanged.

See ``docs/multi-replica-architecture.md`` §Stage 3 for the overall
retention design, and :mod:`cms.services.log_outbox` for the state
machine helpers.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.log_request import (
    STATUS_EXPIRED,
    LogRequest,
)
from cms.services.log_blob import delete_log_blob

logger = logging.getLogger("agora.cms.log_reaper")


# Defaults applied when the caller doesn't pass a Settings object.
# Matches the AGORA_CMS_LOG_REAPER_* fields declared in cms.config.
_DEFAULT_INTERVAL_SEC = 600.0  # 10 minutes — matches the hourly retention
_DEFAULT_BATCH_SIZE = 100


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_postgres(db: AsyncSession) -> bool:
    bind = getattr(db, "bind", None)
    if bind is None:
        try:
            bind = db.get_bind()
        except Exception:
            return False
    return bind.dialect.name == "postgresql"


def _resolve_config(settings: Any) -> int:
    """Return ``batch_size`` from settings."""
    if settings is None:
        return _DEFAULT_BATCH_SIZE
    return int(getattr(settings, "log_reaper_batch_size", _DEFAULT_BATCH_SIZE))


# ── Claim helper ─────────────────────────────────────────────────────


async def claim_expired_rows(
    db: AsyncSession,
    *,
    now: datetime,
    batch_size: int,
) -> list[LogRequest]:
    """Return up to ``batch_size`` rows whose ``expires_at`` has passed
    and that have not yet been marked ``expired``.

    Rows with ``expires_at IS NULL`` (opt-out, e.g. forensic retention)
    are excluded.

    On Postgres the scan uses ``FOR UPDATE SKIP LOCKED`` so multiple
    replicas can reap concurrently without double-deleting blobs.  On
    SQLite (tests) the lock hint is dropped.
    """
    stmt = (
        select(LogRequest)
        .where(
            LogRequest.expires_at.is_not(None),
            LogRequest.expires_at <= now,
            LogRequest.status != STATUS_EXPIRED,
        )
        .order_by(LogRequest.expires_at)
        .limit(batch_size)
        .execution_options(populate_existing=True)
    )
    if _is_postgres(db):
        stmt = stmt.with_for_update(skip_locked=True)
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ── Single tick ──────────────────────────────────────────────────────


BlobDeleter = Callable[[str], Awaitable[bool]]


async def reap_once(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    settings: Any = None,
    blob_deleter: BlobDeleter | None = None,
) -> dict:
    """Run one reaper tick inside a single transaction.

    Returns a stats dict::

        {"claimed": N, "blobs_deleted": D, "blobs_missing": M,
         "rows_expired": E, "errors": K}

    ``blob_deleter`` is injectable for tests; production callers leave
    it ``None`` to use :func:`cms.services.log_blob.delete_log_blob`.

    Commits once at the end.  On any unexpected error the transaction
    is rolled back and the exception propagates so the loop can log
    and continue.
    """
    if now is None:
        now = _now()
    batch_size = _resolve_config(settings)
    deleter: BlobDeleter = blob_deleter or delete_log_blob

    stats = {
        "claimed": 0,
        "blobs_deleted": 0,
        "blobs_missing": 0,
        "rows_expired": 0,
        "errors": 0,
    }

    try:
        rows = await claim_expired_rows(db, now=now, batch_size=batch_size)
        stats["claimed"] = len(rows)

        for row in rows:
            # Attempt blob delete first.  If the delete call errors we
            # leave the row untouched and let the next tick retry —
            # this is safer than marking the row expired and leaking
            # the blob forever.
            if row.blob_path:
                try:
                    existed = await deleter(row.blob_path)
                except Exception:
                    logger.exception(
                        "log_reaper: blob delete failed for %s (row %s)",
                        row.blob_path, row.id,
                    )
                    stats["errors"] += 1
                    continue
                if existed:
                    stats["blobs_deleted"] += 1
                else:
                    stats["blobs_missing"] += 1

            # Flip to expired + null the blob_path.  A row may already
            # be terminal (``ready``, ``failed``); the reaper overrides
            # that because ``expires_at`` is the authoritative retention
            # signal — we want any surviving UI references to see the
            # bundle as cleaned up.
            await db.execute(
                update(LogRequest)
                .where(LogRequest.id == row.id)
                .values(status=STATUS_EXPIRED, blob_path=None, updated_at=_now())
            )
            stats["rows_expired"] += 1

        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    if any(v for v in stats.values()):
        logger.info("reap_once: %s", stats)
    return stats


# ── Loop ─────────────────────────────────────────────────────────────


async def run_loop(
    session_factory_getter: Callable[[], Any],
    *,
    settings: Any,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run :func:`reap_once` forever on a ``log_reaper_interval_sec``
    cadence until ``stop_event`` is set.

    A single tick failure (DB blip, transient blob backend error) is
    logged and the loop keeps going.  ``asyncio.CancelledError`` is
    propagated so the standard shutdown path works.
    """
    interval = float(
        getattr(settings, "log_reaper_interval_sec", _DEFAULT_INTERVAL_SEC)
    )
    if stop_event is None:
        stop_event = asyncio.Event()
    logger.info("Log reaper loop started (interval=%.1fs)", interval)
    try:
        while not stop_event.is_set():
            try:
                factory = session_factory_getter()
                if factory is None:
                    logger.warning(
                        "log_reaper: session factory not initialised; skipping tick",
                    )
                else:
                    async with factory() as db:
                        try:
                            await reap_once(db, settings=settings)
                        except Exception:
                            logger.exception("log_reaper: reap_once failed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("log_reaper: unexpected error in tick")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("Log reaper loop stopped")
