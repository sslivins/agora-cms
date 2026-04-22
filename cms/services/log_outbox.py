"""Log outbox helpers (Stage 3 of #345).

Backs the multi-replica-safe ``request_logs`` flow.  Each helper is a
small atomic operation on the ``log_requests`` outbox table — the
router, the drainer, the upload endpoint and the reaper compose them
into the full lifecycle:

    pending ─► sent ─► ready
                 └─► failed
                 └─► expired

All helpers take an ``AsyncSession`` and **do not commit** — the caller
owns the transaction boundary.  This lets routers batch the outbox
write with audit writes, and the drainer batch status updates with
``FOR UPDATE SKIP LOCKED`` scans.

See ``docs/multi-replica-architecture.md`` §Stage 3 for the locked
design; ``cms/models/log_request.py`` for the schema.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.log_request import (
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_SENT,
    TERMINAL_STATUSES,
    LogRequest,
)

logger = logging.getLogger("agora.cms.log_outbox")


# Default retention for a newly-created log bundle.  Kept short because
# the common case is "user clicks Download and is done" — the download
# handler also sets ``expires_at`` to now on success so the next reaper
# tick cleans the blob up within a few minutes.  The 1 h ceiling is the
# safety net for bundles that are produced but never downloaded.
# Individual callers can override by passing ``expires_in`` explicitly.
DEFAULT_RETENTION = timedelta(hours=1)


def _now() -> datetime:
    return datetime.now(timezone.utc)


async def create(
    db: AsyncSession,
    *,
    device_id: str,
    requested_by_user_id: uuid.UUID | str | None = None,
    services: list[str] | None = None,
    since: str = "24h",
    expires_in: timedelta | None = DEFAULT_RETENTION,
    request_id: str | None = None,
) -> LogRequest:
    """Insert a new ``pending`` outbox row.

    Returns the persisted :class:`LogRequest` (flushed, not committed).
    ``request_id`` is accepted so callers that need to emit the id in a
    response before commit can pre-generate it; otherwise a v4 UUID is
    allocated.
    """
    now = _now()
    row = LogRequest(
        id=request_id or str(uuid.uuid4()),
        device_id=device_id,
        requested_by_user_id=requested_by_user_id,
        services=services,
        since=since,
        status=STATUS_PENDING,
        attempts=0,
        created_at=now,
        updated_at=now,
        expires_at=(now + expires_in) if expires_in is not None else None,
    )
    db.add(row)
    await db.flush()
    return row


async def get(db: AsyncSession, request_id: str) -> LogRequest | None:
    """Fetch a single request by id, or ``None`` when missing."""
    return await db.get(LogRequest, request_id)


async def mark_sent(
    db: AsyncSession,
    request_id: str,
    *,
    attempt_increment: int = 1,
) -> bool:
    """Transition ``pending → sent`` after the transport dispatch
    succeeds.  Records the send timestamp and bumps ``attempts``.

    Returns ``True`` iff a row was updated (i.e. the row still exists
    and was in ``pending``).  Drainer uses the boolean to detect the
    case where another replica picked the same row first — with
    ``SKIP LOCKED`` this should be rare but the guard keeps us honest
    on non-Postgres backends (tests) that don't honour locking.
    """
    result = await db.execute(
        update(LogRequest)
        .where(LogRequest.id == request_id, LogRequest.status == STATUS_PENDING)
        .values(
            status=STATUS_SENT,
            sent_at=_now(),
            updated_at=_now(),
            attempts=LogRequest.attempts + attempt_increment,
        )
    )
    return (result.rowcount or 0) > 0


async def mark_ready(
    db: AsyncSession,
    request_id: str,
    *,
    blob_path: str,
    size_bytes: int,
) -> bool:
    """Transition ``sent → ready`` after the Pi's upload lands in blob
    storage.  Also accepts ``pending → ready`` for the Stage 3b back-
    compat shim that writes the legacy ``LOGS_RESPONSE`` inline
    payload before the drainer has picked up the row.

    Returns ``True`` iff a row was updated.
    """
    result = await db.execute(
        update(LogRequest)
        .where(
            LogRequest.id == request_id,
            LogRequest.status.in_([STATUS_PENDING, STATUS_SENT]),
        )
        .values(
            status=STATUS_READY,
            ready_at=_now(),
            updated_at=_now(),
            blob_path=blob_path,
            size_bytes=size_bytes,
            last_error=None,
        )
    )
    return (result.rowcount or 0) > 0


async def mark_failed(
    db: AsyncSession,
    request_id: str,
    *,
    error: str,
) -> bool:
    """Transition any non-terminal row to ``failed`` with a message.

    Used by the drainer when ``attempts`` exceeds the retry budget and
    by the upload endpoint when Pi reports an error.  Does not bump
    ``attempts`` — that's the drainer's job on send.
    """
    result = await db.execute(
        update(LogRequest)
        .where(
            LogRequest.id == request_id,
            LogRequest.status.notin_(list(TERMINAL_STATUSES)),
        )
        .values(
            status=STATUS_FAILED,
            last_error=error[:2000] if error else None,
            updated_at=_now(),
        )
    )
    return (result.rowcount or 0) > 0


async def mark_expired(
    db: AsyncSession,
    request_id: str,
) -> bool:
    """Flip a row to ``expired`` — used by the reaper before deleting
    its blob.  Only affects non-terminal rows so already-failed or
    already-ready rows keep their final status."""
    result = await db.execute(
        update(LogRequest)
        .where(
            LogRequest.id == request_id,
            LogRequest.status.notin_(list(TERMINAL_STATUSES)),
        )
        .values(status=STATUS_EXPIRED, updated_at=_now())
    )
    return (result.rowcount or 0) > 0


async def record_attempt_error(
    db: AsyncSession,
    request_id: str,
    *,
    error: str,
) -> bool:
    """Record a transient error without changing status.

    Called when the drainer attempts a send that fails but the retry
    budget isn't exhausted.  Bumps ``attempts`` and stores the latest
    error message.  The row stays ``pending`` so the next drainer tick
    picks it up again.
    """
    result = await db.execute(
        update(LogRequest)
        .where(LogRequest.id == request_id, LogRequest.status == STATUS_PENDING)
        .values(
            attempts=LogRequest.attempts + 1,
            last_error=error[:2000] if error else None,
            updated_at=_now(),
        )
    )
    return (result.rowcount or 0) > 0


async def list_pending(
    db: AsyncSession,
    *,
    limit: int = 50,
    max_attempts: int | None = None,
) -> list[LogRequest]:
    """Fetch up to ``limit`` pending rows ordered by ``created_at``.

    Stage 3d will add ``.with_for_update(skip_locked=True)`` on the
    drainer path so multiple replicas can scan concurrently without
    duplicating work.  Stage 3a just exposes the read side; nothing
    calls it yet.
    """
    stmt = (
        select(LogRequest)
        .where(LogRequest.status == STATUS_PENDING)
        .order_by(LogRequest.created_at)
        .limit(limit)
    )
    if max_attempts is not None:
        stmt = stmt.where(LogRequest.attempts < max_attempts)
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_expired(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> list[LogRequest]:
    """Fetch rows whose ``expires_at`` has passed.

    Used by the Stage 3e reaper to delete old blobs.  Terminal rows
    whose blobs are already gone (``blob_path`` IS NULL) are also
    returned so the reaper can drop them from the table.
    """
    cutoff = now or _now()
    stmt = (
        select(LogRequest)
        .where(
            LogRequest.expires_at.is_not(None),
            LogRequest.expires_at <= cutoff,
        )
        .order_by(LogRequest.expires_at)
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def list_for_device(
    db: AsyncSession,
    device_id: str,
    *,
    limit: int = 50,
) -> list[LogRequest]:
    """Return the most recent log requests for one device (UI history)."""
    stmt = (
        select(LogRequest)
        .where(LogRequest.device_id == device_id)
        .order_by(LogRequest.created_at.desc())
        .limit(limit)
    )
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def count_by_status(db: AsyncSession) -> dict[str, int]:
    """Return a ``{status: count}`` dict over all outbox rows.

    Stage 3 observability hook — the Stage 4 smoke-test gate reads
    this to assert the drainer is keeping up.
    """
    stmt = select(LogRequest.status, func.count()).group_by(LogRequest.status)
    result = await db.execute(stmt)
    return {row[0]: row[1] for row in result.all()}
