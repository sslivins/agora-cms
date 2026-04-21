"""Auto-purge stale pending devices."""

import asyncio
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.device import Device, DeviceStatus
from cms.services.transport import get_transport

logger = logging.getLogger("agora.cms.device_purge")


async def purge_stale_pending_devices(db: AsyncSession, ttl_hours: int) -> list[str]:
    """Delete pending devices not seen for longer than *ttl_hours*.

    Returns a list of purged device IDs.
    """
    cutoff = datetime.now(timezone.utc) - timedelta(hours=ttl_hours)

    result = await db.execute(
        select(Device).where(
            Device.status == DeviceStatus.PENDING,
        )
    )
    candidates = result.scalars().all()
    purged: list[str] = []

    for device in candidates:
        # Skip devices that are currently connected
        if get_transport().is_connected(device.id):
            continue

        # Use last_seen, fall back to registered_at
        seen_at = device.last_seen or device.registered_at
        # Ensure both sides are comparable (SQLite strips tzinfo)
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
        if seen_at <= cutoff:
            logger.info("Purging stale pending device %s (last seen %s)", device.id, seen_at)
            await db.delete(device)
            purged.append(device.id)

    if purged:
        await db.commit()

    return purged


async def device_purge_loop() -> None:
    """Background loop that periodically purges stale pending devices."""
    from cms.auth import get_settings
    from cms.database import get_db

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
