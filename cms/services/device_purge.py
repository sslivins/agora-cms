"""Auto-purge stale pending devices."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.device import Device, DeviceStatus

logger = logging.getLogger("agora.cms.device_purge")


async def purge_stale_pending_devices(db: AsyncSession, ttl_hours: int) -> list[str]:
    """Delete pending devices not seen for longer than *ttl_hours*.

    Returns a list of purged device IDs.

    Concurrency / N>1 safety
    ------------------------
    The purge runs under a session-advisory lock so only one replica's
    loop fires at a time, but a user adopting a device on a *different*
    replica can still race with this call between the candidate SELECT
    and the DELETE.  To prevent silently nuking an in-flight adopt, the
    DELETE statement re-checks ``status == PENDING`` and ``online == false``
    in SQL: if the row was flipped to ADOPTED (or the device just
    reconnected) between the two steps, the WHERE clause excludes it
    and the row survives.  We use ``RETURNING id`` to learn which rows
    actually got deleted.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)

    # Step 1 — gather candidates.  Cutoff comparison is done in Python
    # because SQLAlchemy + SQLite ``DateTime(timezone=True)`` round-trips
    # can drop tzinfo, making a SQL ``<= cutoff`` predicate fragile across
    # backends.  Postgres handles it natively, but the same code runs in
    # tests against SQLite, so we keep this part backend-agnostic.
    result = await db.execute(
        select(Device.id, Device.last_seen, Device.registered_at, Device.online)
        .where(Device.status == DeviceStatus.PENDING)
    )

    candidate_ids: list[str] = []
    for did, last_seen, registered_at, online in result.all():
        if online:
            continue
        seen_at = last_seen or registered_at
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
        if seen_at <= cutoff:
            candidate_ids.append(did)

    if not candidate_ids:
        return []

    # Step 2 — atomic guarded DELETE.  Even though candidates were
    # filtered above, the SQL guards re-validate at DELETE time so a
    # concurrent adopt or reconnect on another replica wins the race.
    stmt = (
        delete(Device)
        .where(
            Device.id.in_(candidate_ids),
            Device.status == DeviceStatus.PENDING,
            Device.online == False,  # noqa: E712 — SQLAlchemy comparator
        )
        .returning(Device.id)
    )
    res = await db.execute(stmt)
    purged: list[str] = [row[0] for row in res.all()]

    if purged:
        for did in purged:
            logger.info("Purged stale pending device %s (cutoff %s)", did, cutoff)
        skipped = set(candidate_ids) - set(purged)
        if skipped:
            logger.info(
                "Skipped %d candidate(s) racing with adopt/reconnect: %s",
                len(skipped), sorted(skipped),
            )
        await db.commit()

    return purged


async def device_purge_loop() -> None:
    """Background loop that periodically purges stale pending devices.

    Stage 4 (#344): gated by a session-advisory lock so only one
    replica runs the DELETE pass at a time.  DELETEs are idempotent
    but running them on N replicas at once wastes a DB round-trip.
    """
    from cms.auth import get_settings
    from cms.database import get_db
    from cms.services.leader import session_advisory_lock

    _LOCK_ID = 0x4147_4F52_41_02  # 'AGORA' + 02

    # Wait for startup
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        return

    while True:
        try:
            settings = get_settings()
            ttl = settings.pending_device_ttl_hours
            if ttl > 0:
                async with session_advisory_lock(_LOCK_ID) as got:
                    if got:
                        async for db in get_db():
                            purged = await purge_stale_pending_devices(db, ttl)
                            if purged:
                                logger.info("Purged %d stale pending device(s)", len(purged))
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error in device purge loop")

        try:
            await asyncio.sleep(3600)  # Check every hour
        except asyncio.CancelledError:
            return
