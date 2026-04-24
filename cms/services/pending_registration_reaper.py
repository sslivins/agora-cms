"""Background loop that reaps expired ``pending_registrations`` rows.

Paired with the hard cap in ``register_device`` (``pending_registrations_max``),
this loop prevents the table from filling up indefinitely under
registration spam or buggy firmware that re-registers on a tight loop
without ever polling bootstrap-status.

Gated behind a session-advisory lock so only one replica runs the DELETE
pass at a time — duplicate deletes would be idempotent but waste DB
round-trips.  Per-tick body is :func:`reap_pending_registrations` so
tests can drive it deterministically.
"""

from __future__ import annotations

import asyncio
import logging


logger = logging.getLogger(__name__)


async def pending_registration_reaper_loop() -> None:
    """Background loop — wakes on ``bootstrap_reaper_interval_seconds``."""
    from cms.auth import get_settings
    from cms.database import get_db
    from cms.services.device_bootstrap import reap_pending_registrations
    from cms.services.leader import session_advisory_lock

    _LOCK_ID = 0x4147_4F52_41_05  # 'AGORA' + 05 (01 backfill, 02 device_purge, 03 capture_monitor, 04 asset_reaper)

    # Wait for startup to settle.
    try:
        await asyncio.sleep(30)
    except asyncio.CancelledError:
        return

    while True:
        try:
            settings = get_settings()
            interval = max(60, int(settings.bootstrap_reaper_interval_seconds))
            async with session_advisory_lock(_LOCK_ID) as got:
                if got:
                    async for db in get_db():
                        deleted = await reap_pending_registrations(
                            db=db,
                            unpolled_ttl_seconds=int(
                                settings.bootstrap_reaper_unpolled_ttl_seconds
                            ),
                            polled_ttl_seconds=int(
                                settings.bootstrap_reaper_polled_ttl_seconds
                            ),
                            adopted_ttl_seconds=int(
                                settings.bootstrap_reaper_adopted_ttl_seconds
                            ),
                        )
                        if deleted:
                            logger.info(
                                "Reaped %d expired pending_registrations row(s)",
                                deleted,
                            )
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error in pending_registrations reaper loop")

        try:
            await asyncio.sleep(interval)
        except asyncio.CancelledError:
            return
