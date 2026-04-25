"""Alert service — DB-backed device health monitoring (Stage 4 of #344).

Two independent alert streams, both persisted in ``device_alert_state``
so they survive replica failover and don't double-fire across replicas:

1. **Offline detection** — the disconnect path only transitions
   ``offline_since`` NULL→timestamp (duplicate disconnects don't reset
   the grace window).  A leader-gated sweep loop
   (:func:`offline_sweep_once` driven from ``cms/main.py``) flips
   ``offline_notified`` and emits the "offline" notification + event
   in a single transaction.  The reconnect path CAS-consumes
   ``offline_notified`` in the same transaction that emits the "back
   online" notification, so a race between two replicas handling the
   reconnect only lets one of them fire the alert.

2. **Temperature monitoring** — STATUS heartbeats land on whichever
   replica the WPS webhook is routed to.  ``check_temperature`` locks
   the device's ``device_alert_state`` row via ``SELECT ... FOR
   UPDATE`` and inspects-and-mutates the persisted ``temp_level`` /
   ``temp_last_alert_at`` / ``temp_last_sample_ts`` columns in one
   transaction.  Out-of-order samples (older ``sample_ts`` than the
   one stored) are ignored.  High-temperature alerts are never
   deduped away silently: if a device sits at warning or critical for
   longer than the cooldown, we re-emit a reminder alert so the
   operator is notified every cooldown window.  No leader election is
   needed — the row lock is the serializer.

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


def _upsert_alert_state_stmt(device_id: str):
    """Return a dialect-aware ``INSERT ... ON CONFLICT DO NOTHING`` stmt.

    Used to lazily create a ``device_alert_state`` row for a device the
    first time we need to mutate its temp state.  Postgres path uses
    ``ON CONFLICT DO NOTHING``; sqlite tests use ``OR IGNORE``.
    """
    from cms.database import get_engine
    engine = get_engine()
    dialect = engine.dialect.name if engine is not None else "sqlite"
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        return pg_insert(DeviceAlertState).values(
            device_id=device_id, temp_level="normal",
        ).on_conflict_do_nothing(index_elements=["device_id"])
    # sqlite / other
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    return sqlite_insert(DeviceAlertState).values(
        device_id=device_id, temp_level="normal",
    ).on_conflict_do_nothing(index_elements=["device_id"])


logger = logging.getLogger("agora.cms.alerts")

# Default settings (overridable via CMSSetting)
DEFAULT_OFFLINE_GRACE_SECONDS = 120
DEFAULT_TEMP_WARNING_C = 70.0
DEFAULT_TEMP_CRITICAL_C = 80.0
DEFAULT_TEMP_COOLDOWN_SECONDS = 300

# Stale-presence sweep (PR #440): how long a device may go without a
# STATUS heartbeat before we infer it is offline regardless of the
# WebSocket / WPS connection state. Matches roughly 2 missed 30s
# heartbeats — short enough that the UI flips offline within a minute
# of a power-cut Pi, long enough to absorb a one-off network blip.
STALE_PRESENCE_THRESHOLD_S = 60
# Maximum devices flipped offline in a single sweep tick. Caps the
# cost of a pathological backlog (e.g., a clock-skew bug or a
# datacenter blip flipping the whole fleet at once); the next tick
# will pick up the rest.
STALE_PRESENCE_BATCH_SIZE = 50


class AlertService:
    """Singleton service wired into the WebSocket + WPS webhook handlers.

    All alert state lives in the ``device_alert_state`` table so it
    survives replica failover and doesn't double-fire under N>1.
    """

    def __init__(self):
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

    async def stale_presence_sweep_once(self, db: AsyncSession) -> int:
        """Mark devices that have stopped heartbeating as offline.

        Backstop for the WS-disconnect path under N>1 replicas: when
        Container Apps' load balancer keeps a dead WebSocket "alive"
        for many minutes (or when a device drops off the WPS gateway
        without a clean close), no replica observes the disconnect, so
        ``devices.online`` stays TRUE and ``device_alert_state.offline_since``
        is never written. The offline-alert pipeline therefore can't
        fire and the UI keeps showing the device as healthy.

        This sweep claims any row with ``online = TRUE`` and
        ``last_seen < now - STALE_PRESENCE_THRESHOLD_S`` in a single
        atomic ``UPDATE ... RETURNING``, so two replicas running the
        sweep concurrently can't double-process a device. For each
        adopted+grouped row it emits an OFFLINE event (kind
        ``stale_heartbeat``) and transitions
        ``device_alert_state.offline_since`` from NULL to NOW so the
        existing :func:`offline_sweep_once` can fire the notification
        once the configured grace window elapses.

        Devices that aren't adopted+grouped are still flipped to
        offline (so the UI doesn't lie) but receive no event or alert
        state — pending registrations and orphaned devices have no
        owner group to notify.

        The sweep is idempotent: a device whose row is already
        ``online = FALSE`` won't match the claim predicate. Returns
        the number of devices flipped offline by this call.
        """
        now = datetime.now(timezone.utc)
        cutoff = now - timedelta(seconds=STALE_PRESENCE_THRESHOLD_S)

        # Cap claim size *before* mutation. Using a subquery with
        # LIMIT (rather than slicing the RETURNING result) avoids
        # stranding rows that were flipped offline but never had
        # ``offline_since`` written, which would leave them invisible
        # to the offline-alert pipeline.
        cap_subq = (
            select(Device.id)
            .where(Device.online.is_(True))
            .where(Device.last_seen.is_not(None))
            .where(Device.last_seen < cutoff)
            .limit(STALE_PRESENCE_BATCH_SIZE)
        )
        claim_stmt = (
            update(Device)
            .where(Device.id.in_(cap_subq))
            # Re-assert the claim predicate on the outer UPDATE so
            # races with concurrent reconnects (which flip
            # ``online`` and bump ``last_seen`` from a heartbeat
            # path) can't accidentally re-flip an already-online row.
            .where(Device.online.is_(True))
            .where(Device.last_seen.is_not(None))
            .where(Device.last_seen < cutoff)
            .values(online=False, connection_id=None)
            .returning(
                Device.id,
                Device.name,
                Device.group_id,
                Device.status,
            )
        )
        claimed = (await db.execute(claim_stmt)).all()
        if not claimed:
            return 0

        # Batch-fetch group names for the rows we'll alert on so we
        # don't issue one SELECT per device.
        from cms.models.device import DeviceGroup
        group_ids = {row.group_id for row in claimed if row.group_id}
        group_names: dict = {}
        if group_ids:
            for gid, gname in (
                await db.execute(
                    select(DeviceGroup.id, DeviceGroup.name).where(
                        DeviceGroup.id.in_(group_ids)
                    )
                )
            ).all():
                group_names[gid] = gname or ""

        from sqlalchemy.exc import IntegrityError
        for row in claimed:
            # Only adopted + grouped devices have an owner to alert.
            if row.status != DeviceStatus.ADOPTED or not row.group_id:
                continue

            group_name = group_names.get(row.group_id, "")
            device_name = row.name or row.id

            # 1. OFFLINE event with stale-detection marker so operators
            #    can distinguish heartbeat-timeout offlines from
            #    WS-close offlines in the audit trail.
            db.add(DeviceEvent(
                device_id=row.id,
                device_name=device_name,
                group_id=row.group_id,
                group_name=group_name,
                event_type=DeviceEventType.OFFLINE,
                details={"kind": "stale_heartbeat"},
            ))

            # 2. Transition ``offline_since`` NULL → NOW. Mirrors the
            #    semantics of ``_record_disconnect`` so duplicate
            #    sweep ticks (or a sweep tick racing with a real WS
            #    close) don't reset the grace window.
            existing = (await db.execute(
                select(DeviceAlertState).where(
                    DeviceAlertState.device_id == row.id
                )
            )).scalar_one_or_none()
            if existing is None:
                # No row yet — common path for a freshly-adopted
                # device that has never disconnected. Insert in a
                # SAVEPOINT so a concurrent insert by the WS path
                # (e.g., racing with a near-simultaneous real
                # disconnect) raises IntegrityError that we can
                # treat as benign without poisoning the outer
                # transaction.
                try:
                    async with db.begin_nested():
                        db.add(DeviceAlertState(
                            device_id=row.id,
                            offline_since=now,
                            offline_notified=False,
                        ))
                except IntegrityError:
                    logger.debug(
                        "DeviceAlertState for %s created concurrently "
                        "during stale-presence sweep; ignoring",
                        row.id,
                    )
            elif existing.offline_since is None:
                # Online → offline transition: mark when we first
                # noticed and ensure the notified flag starts clean
                # so the offline_sweep can claim it after grace.
                existing.offline_since = now
                existing.offline_notified = False
            # else: device is already in an offline_since window
            # (e.g., a partial ws disconnect happened and the sweep
            # is catching up). Leave the timestamp + flag alone.

        await db.commit()
        flipped = len(claimed)
        logger.info(
            "Stale-presence sweep: marked %d device(s) offline "
            "(no heartbeat for ≥ %ds)",
            flipped, STALE_PRESENCE_THRESHOLD_S,
        )
        return flipped

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

    # ── Temperature monitoring (DB-backed, multi-replica safe) ──

    def _classify_temp(self, cpu_temp_c: float) -> str:
        """Map a temperature reading to a level string."""
        if cpu_temp_c >= self._temp_critical_c:
            return "critical"
        if cpu_temp_c >= self._temp_warning_c:
            return "warning"
        return "normal"

    async def check_temperature(
        self,
        db: AsyncSession,
        device_id: str,
        cpu_temp_c: Optional[float],
        device_name: str,
        group_id: Optional[str],
        group_name: str,
        status: str,
        sample_ts: Optional[datetime] = None,
    ):
        """Persistently track temperature alert state for a device.

        Called on every STATUS heartbeat.  Serializes concurrent
        replicas via ``SELECT ... FOR UPDATE`` on the
        ``device_alert_state`` row for this device.

        Firing rules (in order of precedence):

        - Out-of-order samples (older ``sample_ts`` than the last
          one stored) are ignored.
        - Ungrouped / non-adopted / missing-reading paths RESET the
          persisted temp state so re-adoption or re-grouping starts
          fresh.  This prevents a device that was at ``warning``,
          got ungrouped, then later regrouped from suppressing the
          first warning in its new scope.
        - Transitions between adjacent levels (normal↔warning,
          warning↔critical, normal↔critical) fire an event +
          notification.
        - A cooldown applies to re-firing *after a cleared event*
          (normal→non-normal): we wait at least ``cooldown`` since
          the last alert before firing again.  Escalations (warning
          →critical) fire immediately regardless of cooldown so we
          never miss the more serious level.
        - If the device stays at ``warning`` or ``critical`` for
          longer than the cooldown, we emit a reminder TEMP_HIGH
          every cooldown.  User requirement: "never miss a high-temp
          alert."

        The bell notification insert + DeviceEvent insert + state
        mutation all happen inside the same ``db.commit()`` so a
        crash between them rolls back cleanly.

        ``sample_ts`` should come from the Azure WPS ``ce-time``
        header (CloudEvents 1.0) when available.  Falls back to
        ``datetime.now(UTC)`` — logged loudly when that happens so
        missing headers can be spotted in prod.
        """
        if cpu_temp_c is None:
            return

        # Ungrouped / non-adopted: reset persisted state so the first
        # alert after re-adoption/regrouping fires cleanly.
        if status != "adopted" or not group_id:
            await self._reset_temp_state(db, device_id)
            return

        if sample_ts is None:
            sample_ts = datetime.now(timezone.utc)
            logger.warning(
                "check_temperature for %s had no sample_ts; using server "
                "now() as fallback (ce-time header likely missing from "
                "WPS webhook)",
                device_id,
            )

        new_level = self._classify_temp(cpu_temp_c)

        # Lazily create the alert-state row; no-op if already present.
        try:
            await db.execute(_upsert_alert_state_stmt(device_id))
        except Exception:
            # The row may already exist; fine.  Other errors (FK
            # violation from a not-yet-created device) bubble below.
            logger.debug("Upsert of device_alert_state row for %s failed "
                         "(likely already exists)", device_id)

        # Lock the row for the duration of this transaction.  Under
        # Postgres this serializes concurrent replicas processing
        # heartbeats for the same device.  Under sqlite it degrades
        # to a no-op but the test suite doesn't race on the same row.
        stmt = (
            select(DeviceAlertState)
            .where(DeviceAlertState.device_id == device_id)
            .with_for_update()
        )
        try:
            result = await db.execute(stmt)
            state = result.scalar_one_or_none()
        except Exception:
            logger.exception(
                "Failed to lock device_alert_state for %s; skipping temp "
                "alert this cycle", device_id,
            )
            return

        if state is None:
            # Upsert failed AND the row doesn't exist — probably the
            # device FK isn't there yet.  Log and bail.
            logger.warning(
                "device_alert_state row for %s unexpectedly missing after "
                "upsert; skipping temp alert",
                device_id,
            )
            return

        # Reject stale samples (out-of-order webhook delivery).  We
        # compare >= so that a duplicate retry with the same ts is a
        # no-op (idempotent).  SQLite strips tzinfo on read, so
        # normalize both sides to naive-UTC for the comparison.  In
        # Postgres both are TIMESTAMPTZ and compare naturally.
        def _to_naive_utc(dt: datetime) -> datetime:
            if dt.tzinfo is not None:
                return dt.astimezone(timezone.utc).replace(tzinfo=None)
            return dt

        if state.temp_last_sample_ts is not None:
            stored = _to_naive_utc(state.temp_last_sample_ts)
            incoming = _to_naive_utc(sample_ts)
            if stored >= incoming:
                return

        old_level = state.temp_level
        cooldown = timedelta(seconds=self._temp_cooldown_seconds)
        if state.temp_last_alert_at is None:
            past_cooldown = True
        else:
            last_alert = _to_naive_utc(state.temp_last_alert_at)
            incoming_naive = _to_naive_utc(sample_ts)
            past_cooldown = last_alert + cooldown <= incoming_naive

        should_fire = False
        event_type: DeviceEventType | None = None
        notif_level = "info"

        if old_level != new_level:
            # Transition.
            if new_level == "normal":
                # Cleared.  Always fire (no cooldown on good news).
                should_fire = True
                event_type = DeviceEventType.TEMP_CLEARED
                notif_level = "success"
            elif old_level == "warning" and new_level == "critical":
                # Escalation — never miss a critical.
                should_fire = True
                event_type = DeviceEventType.TEMP_HIGH
                notif_level = "error"
            else:
                # normal→warning, normal→critical, critical→warning.
                # Apply cooldown.
                if past_cooldown:
                    should_fire = True
                    event_type = DeviceEventType.TEMP_HIGH
                    notif_level = "error" if new_level == "critical" else "warning"
        elif new_level != "normal":
            # Same non-normal level.  Reminder path: re-alert every
            # cooldown window so sustained high temperatures aren't
            # silently swallowed.
            if past_cooldown:
                should_fire = True
                event_type = DeviceEventType.TEMP_HIGH
                notif_level = "error" if new_level == "critical" else "warning"

        # Always advance the monotonic sample timestamp + stored level
        # so future samples see accurate state — even on a no-fire
        # branch (we learned something new even if we don't alert).
        state.temp_level = new_level
        state.temp_last_sample_ts = sample_ts

        if should_fire and event_type is not None:
            state.temp_last_alert_at = sample_ts
            await self._emit_temp_alert(
                db, device_id, device_name, group_id, group_name,
                event_type=event_type,
                notif_level=notif_level,
                cpu_temp_c=cpu_temp_c,
                new_level=new_level,
                previous_level=old_level,
            )

        await db.commit()

    async def _reset_temp_state(self, db: AsyncSession, device_id: str):
        """Clear persisted temp state for a device.

        Called when a STATUS heartbeat arrives for a device that is
        ungrouped, non-adopted, or has no temperature reading.  The
        next valid high-temp reading will then fire normally.

        Uses a plain UPDATE; no-op if the row doesn't exist.  Does
        not commit — caller owns the transaction.
        """
        try:
            stmt = (
                update(DeviceAlertState)
                .where(DeviceAlertState.device_id == device_id)
                .values(
                    temp_level="normal",
                    temp_last_alert_at=None,
                    temp_last_sample_ts=None,
                )
            )
            await db.execute(stmt)
            await db.commit()
        except Exception:
            logger.exception("Failed to reset temp state for %s", device_id)

    async def _emit_temp_alert(
        self,
        db: AsyncSession,
        device_id: str,
        device_name: str,
        group_id: str,
        group_name: str,
        *,
        event_type: DeviceEventType,
        notif_level: str,
        cpu_temp_c: float,
        new_level: str,
        previous_level: str,
    ):
        """Insert a DeviceEvent + Notification for a temperature transition.

        Caller owns the transaction (we only ``db.add``; no commit).
        """
        gid = _to_uuid(group_id)
        details = {
            "cpu_temp_c": cpu_temp_c,
            "threshold_warning": self._temp_warning_c,
            "threshold_critical": self._temp_critical_c,
            "level": new_level,
            "previous_level": previous_level,
        }
        db.add(DeviceEvent(
            device_id=device_id,
            device_name=device_name,
            group_id=gid,
            group_name=group_name,
            event_type=event_type,
            details=details,
        ))

        if event_type == DeviceEventType.TEMP_HIGH:
            title = f"High temperature: {device_name}"
            message = (
                f"Device '{device_name}' in group '{group_name}' "
                f"is at {cpu_temp_c:.1f}°C ({new_level})."
            )
        else:
            title = f"Temperature normal: {device_name}"
            message = (
                f"Device '{device_name}' in group '{group_name}' "
                f"temperature returned to normal ({cpu_temp_c:.1f}°C)."
            )

        db.add(Notification(
            scope="group",
            level=notif_level,
            title=title,
            message=message,
            group_id=gid,
            details={
                "device_id": device_id,
                "event_type": event_type.value if hasattr(event_type, "value") else str(event_type),
                "cpu_temp_c": cpu_temp_c,
                "level": new_level,
                "previous_level": previous_level,
            },
        ))
        logger.info(
            "Temperature %s for device %s (%.1f°C, %s→%s)",
            event_type, device_id, cpu_temp_c, previous_level, new_level,
        )

    # ── Cleanup ──

    def cleanup_device(self, device_id: str):
        """Backward-compat no-op.

        Previously cleaned up the in-memory ``_temp_states`` dict.
        Now that all temp state lives in ``device_alert_state``, the
        ``ON DELETE CASCADE`` on ``device_alert_state.device_id``
        handles cleanup when a device is deleted.
        """
        return


# Singleton
alert_service = AlertService()
