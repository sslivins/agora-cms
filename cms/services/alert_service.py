"""Alert service — DB-backed device health monitoring (Stage 4 of #344).

Two independent alert streams:

1. **Offline detection** — persisted in ``device_alert_state`` so alerts
   survive failover and don't double-fire across replicas.  The
   disconnect path only transitions ``offline_since`` NULL→timestamp
   (duplicate disconnects don't reset the grace window).  A
   leader-gated sweep loop (:func:`offline_sweep_once` driven from
   ``cms/main.py``) flips ``offline_notified`` and emits the
   "offline" notification + event in a single transaction.  The
   reconnect path CAS-consumes ``offline_notified`` in the same
   transaction that emits the "back online" notification, so a race
   between two replicas handling the reconnect only lets one of them
   fire the alert.

2. **Temperature monitoring** — still replica-local.  Temp alerts
   come from STATUS heartbeats; under WPS those land on whichever
   replica the webhook is routed to.  Each replica maintains its own
   cooldown state; duplicate temperature warnings are acceptable (and
   arguably informative), so persisting this state is not a
   correctness requirement for multi-replica.

Notifications use scope="group" so only users with access to the
device's group (plus admins via groups:view_all) see them in the bell.
"""

from __future__ import annotations

import asyncio
import logging
import uuid as _uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.models.device import Device, DeviceStatus
from cms.models.device_alert_state import DeviceAlertState
from cms.models.device_event import DeviceEvent, DeviceEventType
from cms.models.notification import Notification


def _to_uuid(val: str | _uuid.UUID | None) -> _uuid.UUID | None:
    """Coerce a string or UUID to uuid.UUID (needed for UUID columns)."""
    if val is None:
        return None
    return val if isinstance(val, _uuid.UUID) else _uuid.UUID(str(val))


logger = logging.getLogger("agora.cms.alerts")

# Default settings (overridable via CMSSetting)
DEFAULT_OFFLINE_GRACE_SECONDS = 120
DEFAULT_TEMP_WARNING_C = 70.0
DEFAULT_TEMP_CRITICAL_C = 80.0
DEFAULT_TEMP_COOLDOWN_SECONDS = 300


class _TempState:
    """Per-device temperature alert state machine (in-memory, replica-local)."""

    __slots__ = ("level", "last_alert_at")

    def __init__(self):
        self.level: str = "normal"   # "normal", "warning", "critical"
        self.last_alert_at: Optional[datetime] = None


class AlertService:
    """Singleton service wired into the WebSocket handler.

    Offline alerting state lives in the ``device_alert_state`` table so
    it survives replica failover and doesn't double-fire.  Temperature
    alert state is in-memory per replica (see module docstring for
    rationale).
    """

    def __init__(self):
        self._temp_states: dict[str, _TempState] = {}
        # Cached settings (refreshed periodically by
        # ``_alert_settings_refresh_loop`` in ``cms/main.py``).
        self._offline_grace_seconds: int = DEFAULT_OFFLINE_GRACE_SECONDS
        self._temp_warning_c: float = DEFAULT_TEMP_WARNING_C
        self._temp_critical_c: float = DEFAULT_TEMP_CRITICAL_C
        self._temp_cooldown_seconds: int = DEFAULT_TEMP_COOLDOWN_SECONDS
        self._email_enabled: bool = False

    # ── Settings ──

    async def refresh_settings(self):
        """Reload alert settings from the database."""
        try:
            from cms.database import get_db
            from cms.auth import get_setting
            async for db in get_db():
                val = await get_setting(db, "alert_offline_grace_seconds")
                if val is not None:
                    self._offline_grace_seconds = int(val)
                val = await get_setting(db, "alert_temp_warning_c")
                if val is not None:
                    self._temp_warning_c = float(val)
                val = await get_setting(db, "alert_temp_critical_c")
                if val is not None:
                    self._temp_critical_c = float(val)
                val = await get_setting(db, "alert_temp_cooldown_seconds")
                if val is not None:
                    self._temp_cooldown_seconds = int(val)
                val = await get_setting(db, "email_notifications_enabled")
                self._email_enabled = val == "true"
                break
        except Exception:
            logger.debug("Could not refresh alert settings, using defaults")

    @property
    def offline_grace_seconds(self) -> int:
        return self._offline_grace_seconds

    # ── Offline detection (DB-backed) ──

    def device_disconnected(
        self,
        device_id: str,
        device_name: str,
        group_id: Optional[str],
        group_name: str,
        status: str,
    ):
        """Called from ws.py when a device's WebSocket closes.

        Spawns a detached task that (a) logs an OFFLINE event for the
        adopted+grouped device and (b) transitions ``device_alert_state``
        from online→offline by writing ``offline_since = NOW()`` *only
        when it is currently NULL*.  Duplicate disconnects do not reset
        the grace window.  The actual notification is fired later by
        the leader-gated sweep loop once ``offline_since + grace < now``.
        """
        if status != "adopted" or not group_id:
            return

        asyncio.create_task(
            self._record_disconnect(device_id, device_name, group_id, group_name)
        )

    def device_reconnected(
        self,
        device_id: str,
        device_name: str,
        group_id: Optional[str],
        group_name: str,
        status: str,
    ):
        """Called from ws.py after a successful register.

        Spawns a detached task that:
          1. Logs an ONLINE event for adopted+grouped devices.
          2. Atomically consumes ``offline_notified`` — if it was TRUE
             (we'd previously fired an offline alert), emits the
             "back online" notification in the same transaction.
          3. Clears ``offline_since`` so the next disconnect starts a
             fresh grace window.
        """
        if status != "adopted" or not group_id:
            # Still clear any persisted state so orphaning/regrouping
            # doesn't leave stale rows around.
            asyncio.create_task(self._clear_alert_state(device_id))
            return

        asyncio.create_task(
            self._record_reconnect(device_id, device_name, group_id, group_name)
        )

    # ── Internal: disconnect path ──

    async def _record_disconnect(
        self,
        device_id: str,
        device_name: str,
        group_id: str,
        group_name: str,
    ) -> None:
        """Persist the disconnect: OFFLINE event + offline_since transition."""
        try:
            from cms.database import get_db
            gid = _to_uuid(group_id)
            async for db in get_db():
                # 1. Always log an OFFLINE event immediately.
                db.add(DeviceEvent(
                    device_id=device_id,
                    device_name=device_name,
                    group_id=gid,
                    group_name=group_name,
                    event_type=DeviceEventType.OFFLINE,
                ))

                # 2. Transition-only update: set offline_since=NOW() if
                #    currently NULL, otherwise leave it alone.  We use a
                #    SELECT-then-write pattern so it works across
                #    SQLite+Postgres (SQLite's ORM INSERT..ON CONFLICT
                #    support is dialect-fiddly); the transaction isolation
                #    from the surrounding session gives us the
                #    atomicity we need.
                state = (await db.execute(
                    select(DeviceAlertState).where(
                        DeviceAlertState.device_id == device_id
                    )
                )).scalar_one_or_none()
                if state is None:
                    db.add(DeviceAlertState(
                        device_id=device_id,
                        offline_since=datetime.now(timezone.utc),
                        offline_notified=False,
                    ))
                elif state.offline_since is None:
                    state.offline_since = datetime.now(timezone.utc)
                # else: already offline, leave timestamp + notified flag alone

                await db.commit()
                logger.debug(
                    "Offline event + state recorded for device %s", device_id,
                )
                break
        except Exception:
            logger.exception(
                "Failed to record disconnect for device %s", device_id,
            )

    # ── Internal: reconnect path ──

    async def _record_reconnect(
        self,
        device_id: str,
        device_name: str,
        group_id: str,
        group_name: str,
    ) -> None:
        """Persist the reconnect: ONLINE event + CAS-consume offline_notified."""
        try:
            from cms.database import get_db
            gid = _to_uuid(group_id)
            async for db in get_db():
                # 1. Always log an ONLINE event.
                db.add(DeviceEvent(
                    device_id=device_id,
                    device_name=device_name,
                    group_id=gid,
                    group_name=group_name,
                    event_type=DeviceEventType.ONLINE,
                ))

                # 2. Atomically CAS-consume offline_notified.  Only the
                #    replica whose UPDATE lands first gets a returned
                #    row — other replicas handling the same reconnect
                #    event see an empty result and skip the "back
                #    online" notification, so it fires exactly once
                #    across the cluster.
                claim = await db.execute(
                    update(DeviceAlertState)
                    .where(
                        DeviceAlertState.device_id == device_id,
                        DeviceAlertState.offline_notified.is_(True),
                    )
                    .values(offline_since=None, offline_notified=False)
                    .returning(DeviceAlertState.device_id)
                )
                was_notified = claim.scalar_one_or_none() is not None

                if not was_notified:
                    # Either no alert-state row existed, or it existed
                    # but offline_notified was already False.  Ensure
                    # the row exists with offline_since cleared so the
                    # next disconnect starts a fresh grace window.
                    existing = (await db.execute(
                        select(DeviceAlertState).where(
                            DeviceAlertState.device_id == device_id
                        )
                    )).scalar_one_or_none()
                    if existing is None:
                        # Two replicas can reach this branch
                        # simultaneously for the same device's first
                        # reconnect.  The second INSERT would violate
                        # the PK constraint and poison the outer
                        # transaction (rolling back our ONLINE event).
                        # Isolate the INSERT in a SAVEPOINT and treat
                        # IntegrityError as benign — the other replica
                        # already created the row.
                        from sqlalchemy.exc import IntegrityError
                        try:
                            async with db.begin_nested():
                                db.add(DeviceAlertState(
                                    device_id=device_id,
                                    offline_since=None,
                                    offline_notified=False,
                                ))
                        except IntegrityError:
                            logger.debug(
                                "DeviceAlertState for %s created concurrently; ignoring",
                                device_id,
                            )
                    elif existing.offline_since is not None:
                        existing.offline_since = None

                # 3. If we'd previously fired an offline notification,
                #    emit the matching back-online notification in the
                #    same transaction — so a crash between commit and
                #    INSERT can't lose the signal.
                if was_notified:
                    db.add(Notification(
                        scope="group",
                        level="success",
                        title=f"Device back online: {device_name}",
                        message=(
                            f"Device '{device_name}' in group '{group_name}' "
                            f"is back online."
                        ),
                        group_id=gid,
                        details={
                            "device_id": device_id,
                            "event_type": "online",
                        },
                    ))

                await db.commit()
                if was_notified:
                    logger.info(
                        "Online notification created for device %s", device_id,
                    )
                else:
                    logger.debug(
                        "Online event recorded for device %s", device_id,
                    )
                break
        except Exception:
            logger.exception(
                "Failed to record reconnect for device %s", device_id,
            )

    async def _clear_alert_state(self, device_id: str) -> None:
        """Clear persisted alert state for a non-adopted/ungrouped device."""
        try:
            from cms.database import get_db
            async for db in get_db():
                await db.execute(
                    update(DeviceAlertState)
                    .where(DeviceAlertState.device_id == device_id)
                    .values(offline_since=None, offline_notified=False)
                )
                await db.commit()
                break
        except Exception:
            logger.debug("Best-effort clear of alert state for %s failed", device_id)

    # ── Leader-gated sweep (fires offline notifications past grace) ──

    async def offline_sweep_once(self, db: AsyncSession) -> int:
        """Fire offline notifications for any device past the grace period.

        Safe to call from multiple replicas concurrently: the claim
        step is a single ``UPDATE ... RETURNING`` that atomically flips
        ``offline_notified`` from FALSE→TRUE for all due rows.  Only
        one replica's UPDATE returns any given row; other replicas'
        UPDATEs find nothing matching and emit no duplicate alerts.

        The loop is still wrapped in :class:`LeaderLease` in
        :func:`cms.main._offline_sweep_loop` as a belt-and-braces
        efficiency gate (avoids N replicas all scanning the table
        every tick), but correctness no longer depends on the lease.

        Returns the number of notifications emitted.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(
            seconds=self._offline_grace_seconds
        )

        # Atomic claim: flip offline_notified=FALSE→TRUE for every
        # device past the grace window in a single statement.  The
        # RETURNING clause gives us only the rows this replica won.
        claimed_ids = (await db.execute(
            update(DeviceAlertState)
            .where(
                DeviceAlertState.offline_notified.is_(False),
                DeviceAlertState.offline_since.is_not(None),
                DeviceAlertState.offline_since <= cutoff,
            )
            .values(offline_notified=True)
            .returning(DeviceAlertState.device_id)
        )).scalars().all()
        if not claimed_ids:
            return 0

        emitted = 0
        for device_id in claimed_ids:
            device = (await db.execute(
                select(Device)
                .options(selectinload(Device.group))
                .where(Device.id == device_id)
            )).scalar_one_or_none()
            if not device or device.status != DeviceStatus.ADOPTED or not device.group_id:
                # Device is no longer adopted/grouped — we've already
                # flipped offline_notified to TRUE via the claim, so
                # it won't be re-scanned.  No alert fired.
                continue

            group_name = device.group.name if device.group else ""
            device_name = device.name or device.id

            db.add(DeviceEvent(
                device_id=device.id,
                device_name=device_name,
                group_id=device.group_id,
                group_name=group_name,
                event_type=DeviceEventType.OFFLINE,
                details={"kind": "grace_expired"},
            ))
            db.add(Notification(
                scope="group",
                level="error",
                title=f"Device offline: {device_name}",
                message=(
                    f"Device '{device_name}' in group '{group_name}' has "
                    f"been offline for over {self._offline_grace_seconds} "
                    f"seconds."
                ),
                group_id=device.group_id,
                details={
                    "device_id": device.id,
                    "event_type": "offline",
                },
            ))
            emitted += 1
            logger.info(
                "Offline notification emitted for device %s (%s)",
                device.id, device_name,
            )

        await db.commit()
        return emitted

    # ── Temperature monitoring (replica-local, unchanged) ──

    def check_temperature(
        self,
        device_id: str,
        cpu_temp_c: Optional[float],
        device_name: str,
        group_id: Optional[str],
        group_name: str,
        status: str,
    ):
        """Called on every STATUS heartbeat.  Only alerts for adopted+grouped devices."""
        if cpu_temp_c is None or status != "adopted" or not group_id:
            return

        state = self._temp_states.get(device_id)
        if state is None:
            state = _TempState()
            self._temp_states[device_id] = state

        # Determine new level
        if cpu_temp_c >= self._temp_critical_c:
            new_level = "critical"
        elif cpu_temp_c >= self._temp_warning_c:
            new_level = "warning"
        else:
            new_level = "normal"

        if new_level == state.level:
            return  # No transition

        old_level = state.level
        now = datetime.now(timezone.utc)

        # Cooldown: don't re-alert within cooldown after a cleared event
        if (
            old_level == "normal"
            and new_level != "normal"
            and state.last_alert_at
        ):
            elapsed = (now - state.last_alert_at).total_seconds()
            if elapsed < self._temp_cooldown_seconds:
                return

        state.level = new_level

        if new_level == "normal" and old_level != "normal":
            # Temperature cleared
            state.last_alert_at = now
            asyncio.create_task(
                self._create_temp_event(
                    device_id, device_name, group_id, group_name,
                    DeviceEventType.TEMP_CLEARED, cpu_temp_c, old_level,
                )
            )
        elif new_level != "normal":
            # Temperature high (warning or critical)
            state.last_alert_at = now
            asyncio.create_task(
                self._create_temp_event(
                    device_id, device_name, group_id, group_name,
                    DeviceEventType.TEMP_HIGH, cpu_temp_c, new_level,
                )
            )

    async def _create_temp_event(
        self,
        device_id: str,
        device_name: str,
        group_id: str,
        group_name: str,
        event_type: DeviceEventType,
        cpu_temp_c: float,
        level: str,
    ):
        """Create a temperature event + notification."""
        try:
            from cms.database import get_db
            gid = _to_uuid(group_id)
            async for db in get_db():
                event = DeviceEvent(
                    device_id=device_id,
                    device_name=device_name,
                    group_id=gid,
                    group_name=group_name,
                    event_type=event_type,
                    details={
                        "cpu_temp_c": cpu_temp_c,
                        "threshold_warning": self._temp_warning_c,
                        "threshold_critical": self._temp_critical_c,
                        "level": level,
                    },
                )
                db.add(event)

                if event_type == DeviceEventType.TEMP_HIGH:
                    notif_level = "error" if level == "critical" else "warning"
                    title = f"High temperature: {device_name}"
                    message = (
                        f"Device '{device_name}' in group '{group_name}' "
                        f"is at {cpu_temp_c:.1f}°C ({level})."
                    )
                else:
                    notif_level = "success"
                    title = f"Temperature normal: {device_name}"
                    message = (
                        f"Device '{device_name}' in group '{group_name}' "
                        f"temperature returned to normal ({cpu_temp_c:.1f}°C)."
                    )

                notification = Notification(
                    scope="group",
                    level=notif_level,
                    title=title,
                    message=message,
                    group_id=gid,
                    details={
                        "device_id": device_id,
                        "event_type": event_type,
                        "cpu_temp_c": cpu_temp_c,
                    },
                )
                db.add(notification)
                await db.commit()
                logger.info(
                    "Temperature %s notification for device %s (%.1f°C)",
                    event_type, device_id, cpu_temp_c,
                )
                break
        except Exception:
            logger.exception("Failed to create temp event for device %s", device_id)

    # ── Cleanup ──

    def cleanup_device(self, device_id: str):
        """Remove in-memory temp state for a device (e.g. when deleted).

        DB-backed offline state is cleaned up by the ``ON DELETE CASCADE``
        on ``device_alert_state.device_id``.
        """
        self._temp_states.pop(device_id, None)


# Singleton
alert_service = AlertService()
