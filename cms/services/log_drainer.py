"""Background drainer for the ``log_requests`` outbox (Stage 3d of #345).

One asyncio loop per CMS replica.  Each tick opens a short-lived
session and does two independent jobs inside one transaction:

1. **Retry pending rows** — rows whose immediate dispatch from the
   router failed (Pi offline, or in future N>1 the Pi's WS is on a
   different replica).  Exponential backoff keyed off ``attempts``:

       delay_seconds = min(60 * 2**attempts, 3600)

   When ``attempts >= max_attempts`` (default 10) we transition to
   ``failed`` via :func:`cms.services.log_outbox.mark_failed`.

2. **Rescue stuck ``sent`` rows** — rows whose ``sent_at`` is more
   than ``sent_timeout_sec`` (default 900s / 15 min) in the past.
   Flip them back to ``pending`` so the next tick re-dispatches.
   If ``attempts`` is already at the retry budget, transition
   straight to ``failed``.

The router's immediate dispatch in ``POST /api/logs/requests`` stays
as-is — this loop is recovery only.

Multi-replica safety: on Postgres each claim uses
``SELECT ... FOR UPDATE SKIP LOCKED`` so overlapping scans don't
double-dispatch.  On SQLite (tests) the lock hint is dropped — tests
are single-process so contention isn't a concern.

See ``docs/multi-replica-architecture.md`` §Stage 3 for the overall
design and ``cms/services/log_outbox.py`` for the state-machine
helpers this module composes.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.log_request import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_SENT,
    LogRequest,
)
from cms.services import log_outbox

logger = logging.getLogger("agora.cms.log_drainer")


# Defaults applied when the caller doesn't pass a Settings object.
# Matches the AGORA_CMS_LOG_DRAINER_* fields declared in cms.config.
_DEFAULT_BATCH_SIZE = 25
_DEFAULT_SENT_TIMEOUT_SEC = 900
_DEFAULT_MAX_ATTEMPTS = 10
_DEFAULT_INTERVAL_SEC = 5.0
_DISPATCH_TIMEOUT_SEC = 10.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _is_postgres(db: AsyncSession) -> bool:
    """Return ``True`` when the session is bound to a Postgres engine.

    Drives the ``SKIP LOCKED`` branch — SQLite doesn't understand the
    locking hint so the tests use a plain ``SELECT``.
    """
    bind = getattr(db, "bind", None)
    if bind is None:
        try:
            bind = db.get_bind()
        except Exception:
            return False
    return bind.dialect.name == "postgresql"


def _backoff_seconds(attempts: int) -> int:
    """Return the retry delay after ``attempts`` failed sends."""
    return min(60 * (2 ** attempts), 3600)


def _as_utc(dt: datetime) -> datetime:
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def _eligible_for_retry(row: LogRequest, now: datetime) -> bool:
    """Whether ``row`` is past its backoff window."""
    if row.attempts == 0:
        return True
    updated = row.updated_at
    if updated is None:
        return True
    next_at = _as_utc(updated) + timedelta(seconds=_backoff_seconds(row.attempts))
    return next_at <= _as_utc(now)


def _resolve_config(settings: Any) -> tuple[int, int, int]:
    """Return ``(batch_size, sent_timeout_sec, max_attempts)`` from settings."""
    if settings is None:
        return _DEFAULT_BATCH_SIZE, _DEFAULT_SENT_TIMEOUT_SEC, _DEFAULT_MAX_ATTEMPTS
    return (
        int(getattr(settings, "log_drainer_batch_size", _DEFAULT_BATCH_SIZE)),
        int(getattr(settings, "log_drainer_sent_timeout_sec", _DEFAULT_SENT_TIMEOUT_SEC)),
        int(getattr(settings, "log_drainer_max_attempts", _DEFAULT_MAX_ATTEMPTS)),
    )


# ── Claim helpers ────────────────────────────────────────────────────


async def claim_due_pending(
    db: AsyncSession,
    *,
    now: datetime,
    batch_size: int,
) -> list[LogRequest]:
    """Return up to ``batch_size`` pending rows that are past their
    backoff window.

    The SQL filter is loose — it pulls the oldest pending rows and
    then filters backoff-eligibility in Python.  Matches the spec's
    "simpler path is fine" guidance: the batch_size cap bounds cost
    and older rows sort first so backoff'd rows rarely starve.

    On Postgres the scan uses ``FOR UPDATE SKIP LOCKED`` so multiple
    replicas can run concurrently without double-dispatch.  On other
    dialects (SQLite in tests) the hint is dropped.
    """
    stmt = (
        select(LogRequest)
        .where(LogRequest.status == STATUS_PENDING)
        .order_by(LogRequest.created_at)
        .limit(batch_size)
        .execution_options(populate_existing=True)
    )
    if _is_postgres(db):
        stmt = stmt.with_for_update(skip_locked=True)
    result = await db.execute(stmt)
    rows = list(result.scalars().all())
    return [r for r in rows if _eligible_for_retry(r, now)]


async def claim_stuck_sent(
    db: AsyncSession,
    *,
    now: datetime,
    sent_timeout_sec: int,
    batch_size: int,
) -> list[LogRequest]:
    """Return up to ``batch_size`` ``sent`` rows whose ``sent_at`` is
    older than ``now - sent_timeout_sec``.

    Rows whose Pi never uploaded (replica crash after send, or Pi
    died mid-upload) pile up in ``sent`` indefinitely without this
    rescue path.  See :func:`drain_once` for the flip-to-pending /
    flip-to-failed logic.
    """
    cutoff = _as_utc(now) - timedelta(seconds=sent_timeout_sec)
    stmt = (
        select(LogRequest)
        .where(LogRequest.status == STATUS_SENT)
        .where(LogRequest.sent_at.is_not(None))
        .where(LogRequest.sent_at <= cutoff)
        .order_by(LogRequest.sent_at)
        .limit(batch_size)
        .execution_options(populate_existing=True)
    )
    if _is_postgres(db):
        stmt = stmt.with_for_update(skip_locked=True)
    result = await db.execute(stmt)
    return list(result.scalars().all())


# ── Single tick ──────────────────────────────────────────────────────


async def drain_once(
    db: AsyncSession,
    *,
    transport: Any,
    now: datetime | None = None,
    settings: Any = None,
) -> dict:
    """Run one drainer tick inside a single transaction.

    Returns a stats dict::

        {"claimed_pending": N, "dispatched": M, "failed": K, "rescued_sent": R}

    Order of operations:

    1. Claim due-pending rows (backoff-eligible).
    2. Claim stuck-sent rows and flip each inline to ``pending`` (or
       ``failed`` if over the retry budget).  Rescued rows are **not**
       re-dispatched in the same tick — they ride the next tick's
       pending scan.  Keeps the transaction short.
    3. Dispatch the pending batch concurrently with
       ``asyncio.gather(return_exceptions=True)`` and a 10s per-row
       timeout.  On success → ``mark_sent``.  On exception →
       ``mark_failed`` if ``attempts + 1`` hits the budget, else
       ``record_attempt_error``.

    Commits once at the end of the ``db.begin()`` block — the helpers
    in ``log_outbox`` are transaction-agnostic and never commit.
    """
    if now is None:
        now = _now()
    batch_size, sent_timeout_sec, max_attempts = _resolve_config(settings)

    stats = {
        "claimed_pending": 0,
        "dispatched": 0,
        "failed": 0,
        "rescued_sent": 0,
    }

    # We own the transaction boundary for this tick: one commit at the
    # end, rollback on unexpected error.  Using ``async with
    # db.begin()`` would race with any autobegun read transaction the
    # caller's session already has open (common in tests that read
    # back state between ticks), so we commit manually to stay robust
    # on reused sessions.
    try:
        pending_rows = await claim_due_pending(
            db, now=now, batch_size=batch_size,
        )
        stats["claimed_pending"] = len(pending_rows)

        stuck_rows = await claim_stuck_sent(
            db,
            now=now,
            sent_timeout_sec=sent_timeout_sec,
            batch_size=batch_size,
        )
        for row in stuck_rows:
            if row.attempts >= max_attempts:
                await log_outbox.mark_failed(
                    db,
                    row.id,
                    error=f"sent timeout after {row.attempts} attempts",
                )
                stats["failed"] += 1
            else:
                await db.execute(
                    update(LogRequest)
                    .where(
                        LogRequest.id == row.id,
                        LogRequest.status == STATUS_SENT,
                    )
                    .values(status=STATUS_PENDING, updated_at=_now())
                )
                stats["rescued_sent"] += 1

        if pending_rows:
            # Snapshot the attributes we need before dispatching — the
            # gathered coroutines only see plain tuples so nothing
            # racy happens to ORM state while transports run.
            targets = [
                (r.id, r.device_id, r.services, r.since, r.attempts)
                for r in pending_rows
            ]

            async def _dispatch(device_id, rid, services, since) -> None:
                await asyncio.wait_for(
                    transport.dispatch_request_logs(
                        device_id,
                        request_id=rid,
                        services=services,
                        since=since,
                    ),
                    timeout=_DISPATCH_TIMEOUT_SEC,
                )

            outcomes = await asyncio.gather(
                *(
                    _dispatch(device_id, rid, services, since)
                    for (rid, device_id, services, since, _attempts) in targets
                ),
                return_exceptions=True,
            )
            for (rid, _device_id, _services, _since, attempts), outcome in zip(
                targets, outcomes,
            ):
                if isinstance(outcome, BaseException):
                    err = str(outcome) or type(outcome).__name__
                    if attempts + 1 >= max_attempts:
                        await log_outbox.mark_failed(db, rid, error=err)
                        stats["failed"] += 1
                    else:
                        await log_outbox.record_attempt_error(
                            db, rid, error=err,
                        )
                else:
                    await log_outbox.mark_sent(db, rid)
                    stats["dispatched"] += 1

        await db.commit()
    except BaseException:
        await db.rollback()
        raise

    if any(v for v in stats.values()):
        logger.info("drain_once: %s", stats)
    return stats


# ── Loop ─────────────────────────────────────────────────────────────


async def run_loop(
    session_factory_getter: Callable[[], Any],
    transport_getter: Callable[[], Any],
    *,
    settings: Any,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Run :func:`drain_once` forever on a ``log_drainer_interval_sec``
    cadence until ``stop_event`` is set.

    Both ``session_factory_getter`` and ``transport_getter`` are
    resolved **per tick** rather than captured at startup — tests
    call :func:`cms.services.transport.set_transport` after boot and
    the factory isn't initialised until ``init_db`` runs.

    A single tick failure (DB blip, transport exception) is logged
    and the loop keeps going.  ``asyncio.CancelledError`` is
    propagated so the standard shutdown path works.
    """
    interval = float(getattr(settings, "log_drainer_interval_sec", _DEFAULT_INTERVAL_SEC))
    if stop_event is None:
        stop_event = asyncio.Event()
    logger.info("Log drainer loop started (interval=%.1fs)", interval)
    try:
        while not stop_event.is_set():
            try:
                factory = session_factory_getter()
                if factory is None:
                    logger.warning(
                        "log_drainer: session factory not initialised; skipping tick",
                    )
                else:
                    async with factory() as db:
                        try:
                            await drain_once(
                                db,
                                transport=transport_getter(),
                                settings=settings,
                            )
                        except Exception:
                            logger.exception("log_drainer: drain_once failed")
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("log_drainer: unexpected error in tick")
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
            except asyncio.TimeoutError:
                pass
    finally:
        logger.info("Log drainer loop stopped")
