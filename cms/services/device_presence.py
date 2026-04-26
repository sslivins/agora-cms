"""DB-backed device presence + telemetry helpers (Stage 2c of #344).

Replaces the in-memory ``device_manager`` state with updates against the
``devices`` table so every CMS replica sees the same presence /
telemetry view.  See :doc:`docs/multi-replica-architecture.md` on the
``docs/multi-replica-plan`` branch for the locked design.

The helpers are intentionally session-scoped: they take an
``AsyncSession`` and commit before returning.  Callers that already hold
a session (the ``/ws/device`` handler, the WPS webhook) pass theirs in;
background tasks that don't use ``_session_factory_fallback`` below.

``update_status`` enforces a monotonic guard on ``devices.last_status_ts``
so duplicate or out-of-order STATUS deliveries can't rewind the visible
state.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from sqlalchemy import case, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.device import Device, DeviceGroup

logger = logging.getLogger("agora.cms.device_presence")


def _normalize_display_ports(value: Any) -> list[dict] | None:
    """Coerce a STATUS-message ``display_ports`` value into the canonical
    ``list[{"name": str, "connected": bool}]`` shape, or ``None``.

    The protocol (``cms.schemas.protocol.PortStatus``, added in PR #455)
    defines per-HDMI-port entries as ``{name, connected}`` dicts.  But
    ``device_inbound`` passes the raw STATUS dict here without Pydantic
    validation, so a misbehaving client (e.g. an outdated simulator that
    emits ``["HDMI-A-1"]``) can poison the ``display_ports`` JSON column,
    later breaking ``DeviceOut`` serialization with a 500 on every
    ``GET /api/devices``.

    Returns ``None`` for any input that isn't a list of dicts with at
    least a ``name`` key — the caller should treat that as "device sent
    nothing usable".  A warning is logged once so we surface protocol
    drift without spamming on every heartbeat.
    """
    if value is None:
        return None
    if not isinstance(value, list):
        logger.warning(
            "ignoring non-list display_ports: %r", type(value).__name__
        )
        return None
    out: list[dict] = []
    for item in value:
        if not isinstance(item, dict) or "name" not in item:
            logger.warning(
                "ignoring malformed display_ports entry: %r", item
            )
            return None
        out.append(item)
    return out


# Columns returned by :func:`list_states`— matches the keys the old
# ``DeviceManager.get_all_states()`` used to emit so the UI / scheduler /
# alert code can keep reading the same dict shape.
_STATE_COLUMNS = (
    Device.id,
    Device.mode,
    Device.asset,
    Device.pipeline_state,
    Device.playback_started_at,
    Device.playback_position_ms,
    Device.uptime_seconds,
    Device.last_seen,
    Device.cpu_temp_c,
    Device.load_avg,
    Device.error,
    Device.error_since,
    Device.ssh_enabled,
    Device.local_api_enabled,
    Device.display_connected,
    Device.display_ports,
    Device.connection_id,
    Device.online,
    Device.ip_address,
)


def _row_to_state(row: Any) -> dict[str, Any]:
    """Convert a row from the ``_STATE_COLUMNS`` select into a dict
    with the keys the rest of the app expects."""
    started_at = row.playback_started_at
    last_seen = row.last_seen
    error_since = row.error_since
    return {
        "device_id": row.id,
        "mode": row.mode,
        "asset": row.asset,
        "pipeline_state": row.pipeline_state,
        "started_at": started_at.isoformat() if started_at else None,
        "playback_position_ms": row.playback_position_ms,
        "uptime_seconds": row.uptime_seconds or 0,
        # ``connected_at`` has no dedicated column — the closest DB signal
        # is ``last_seen`` (bumped on register + every STATUS).  The UI
        # uses it for "connected for X" display only; exact semantics
        # match closely enough that the template doesn't need a change.
        "connected_at": last_seen.isoformat() if last_seen else None,
        "cpu_temp_c": row.cpu_temp_c,
        "load_avg": row.load_avg,
        # IP is now a column — populated by whichever replica processed
        # the most recent register.  May be None for WPS-originated
        # connections or devices that haven't reconnected since Stage 2c.
        "ip_address": row.ip_address,
        "error": row.error,
        "error_since": error_since.isoformat() if error_since else None,
        "ssh_enabled": row.ssh_enabled,
        "local_api_enabled": row.local_api_enabled,
        "display_connected": row.display_connected,
        "display_ports": _normalize_display_ports(row.display_ports),
        "connection_id": row.connection_id,
        "online": bool(row.online),
    }


async def mark_online(
    db: AsyncSession,
    device_id: str,
    connection_id: str | None = None,
    ip_address: str | None = None,
) -> None:
    """Flip ``devices.online`` to ``true`` for *device_id*.

    Also refreshes ``last_seen`` so the "last heard from" display on
    the UI is immediately accurate after reconnect.  ``connection_id``
    is persisted when the caller has one (WPS webhook path); direct-WS
    connections don't expose a stable id and pass ``None``.
    ``ip_address`` is persisted when the caller has it (direct-WS
    register and WPS ``register`` user-message paths); the system
    ``sys.connected`` event has no body to source one from and passes
    ``None``, in which case the column keeps its previous value (so
    the last-known IP survives an offline blip).
    """
    now = datetime.now(timezone.utc)
    values: dict[str, Any] = {
        "online": True,
        "connection_id": connection_id,
        "last_seen": now,
    }
    # Only overwrite ip_address when we have one — otherwise preserve
    # the last-known value.
    if ip_address is not None:
        values["ip_address"] = ip_address
    await db.execute(
        update(Device).where(Device.id == device_id).values(**values)
    )
    await db.commit()


async def mark_offline(
    db: AsyncSession,
    device_id: str,
    *,
    expected_connection_id: str | None = None,
) -> bool:
    """Flip ``devices.online`` to ``false`` and clear ``connection_id``.

    When *expected_connection_id* is provided, the write is guarded by
    the current ``connection_id`` column — the flip only takes effect
    if the stored value matches.  This protects against stale
    disconnects (an old socket closing on replica A after the device
    has already reconnected on replica B) from flipping the fresh
    connection offline.  Returns ``True`` if the row was updated,
    ``False`` when the guard rejected the write.

    When *expected_connection_id* is ``None``, the clear is
    unconditional (matches the pre-Stage-4 behaviour and the WPS
    webhook path which doesn't have a stable token to compare).
    """
    stmt = update(Device).where(Device.id == device_id)
    if expected_connection_id is not None:
        stmt = stmt.where(Device.connection_id == expected_connection_id)
    stmt = stmt.values(online=False, connection_id=None)
    result = await db.execute(stmt)
    await db.commit()
    return (result.rowcount or 0) > 0


async def mark_offline_and_alert(
    db: AsyncSession,
    device_id: str,
    *,
    expected_connection_id: str | None,
) -> bool:
    """CAS-flip presence offline AND fire ``alert_service.device_disconnected``.

    Designed for transport send-failure paths and the WPS
    ``sys.disconnected`` webhook — anywhere we need to react to "this
    device just dropped" by both clearing presence and emitting the
    OFFLINE alert.

    *expected_connection_id* must be the ``connection_id`` snapshotted
    at the moment we last knew the connection was good (send-time for
    transports, the CloudEvent header for the webhook).  The flip uses
    that token as a CAS guard so a stale failure can't knock a fresh
    connection on another replica offline (issue #406 + Stage 4 of
    #344).

    Pass ``None`` only when the caller has no trustworthy token.  In
    that case the flip is best-effort (unconditional) and **no alert is
    fired** — failing closed on alerts is safer than producing duplicate
    OFFLINE events when we can't tell the stale case from the real one.

    Returns ``True`` iff this call performed the transition (and
    therefore dispatched the alert).
    """
    if expected_connection_id is None:
        await mark_offline(db, device_id)
        return False

    # Atomic CAS that returns the alert payload from the same row in
    # one round-trip — avoids a TOCTOU between read-then-flip.
    result = await db.execute(
        update(Device)
        .where(Device.id == device_id)
        .where(Device.connection_id == expected_connection_id)
        .values(online=False, connection_id=None)
        .returning(Device.name, Device.group_id, Device.status)
    )
    row = result.first()
    await db.commit()
    if row is None:
        logger.info(
            "Send-failure offline flip suppressed for %s "
            "(connection_id replaced)", device_id,
        )
        return False

    group_name = ""
    if row.group_id is not None:
        g = await db.execute(
            select(DeviceGroup.name).where(DeviceGroup.id == row.group_id)
        )
        group_name = g.scalar_one_or_none() or ""

    try:
        from cms.services.alert_service import alert_service
        alert_service.device_disconnected(
            device_id,
            device_name=row.name or device_id,
            group_id=str(row.group_id) if row.group_id else None,
            group_name=group_name,
            status=(
                row.status.value
                if hasattr(row.status, "value")
                else str(row.status)
            ),
        )
    except Exception:
        logger.exception(
            "Failed to dispatch disconnect alert for %s", device_id,
        )
    return True


def _parse_timestamp(value: Any) -> datetime | None:
    """Best-effort parse of an ISO-8601 timestamp from the device wire."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value)
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


async def update_status(
    db: AsyncSession,
    device_id: str,
    status: Mapping[str, Any],
    *,
    status_ts: datetime | None = None,
) -> bool:
    """Persist a STATUS heartbeat against the ``devices`` row.

    Returns ``True`` when the row was updated, ``False`` when the update
    was skipped by the monotonic guard (an older or duplicate STATUS).

    The guard compares *status_ts* (defaults to ``now()``) against
    ``devices.last_status_ts`` — only rows where the stored value is
    NULL or strictly older are updated.  This keeps at-most-one-write
    semantics even when multiple replicas receive the same WPS webhook
    event fan-out.

    ``error_since`` is computed inside the UPDATE via a CASE expression
    so the "latch on first error" semantics work against the *previous*
    row state without needing a read-then-write round trip.
    """
    ts = status_ts or datetime.now(timezone.utc)

    new_error = status.get("error")
    # ``error_since`` latches on the first observed error and clears
    # only when the device reports no error again.  Using CASE against
    # ``Device.error`` references the pre-UPDATE value.
    if new_error is None:
        error_since_expr: Any = None
    else:
        error_since_expr = case(
            (Device.error.is_(None), ts),
            else_=Device.error_since,
        )

    started_at = _parse_timestamp(status.get("started_at"))

    values: dict[str, Any] = {
        "last_status_ts": ts,
        "mode": status.get("mode", "unknown"),
        "asset": status.get("asset"),
        "pipeline_state": status.get("pipeline_state", "NULL"),
        "playback_started_at": started_at,
        "playback_position_ms": status.get("playback_position_ms"),
        "uptime_seconds": status.get("uptime_seconds", 0) or 0,
        "cpu_temp_c": status.get("cpu_temp_c"),
        "load_avg": status.get("load_avg"),
        "error": new_error,
        "error_since": error_since_expr,
        "last_seen": ts,
    }
    # Only overwrite nullable toggles when the device supplied a value —
    # matches the old ``DeviceManager.update_status`` "stickiness" for
    # ssh_enabled / local_api_enabled so a STATUS without those fields
    # doesn't clear an optimistic UI toggle.
    if "ssh_enabled" in status and status["ssh_enabled"] is not None:
        values["ssh_enabled"] = status["ssh_enabled"]
    if "local_api_enabled" in status and status["local_api_enabled"] is not None:
        values["local_api_enabled"] = status["local_api_enabled"]
    # ``display_connected`` can legitimately toggle False — pass through
    # as-is (including None, which means "unknown on this device").
    if "display_connected" in status:
        values["display_connected"] = status["display_connected"]
    # ``display_ports`` is the per-HDMI-port array reported by Pi 5 / Pi 4
    # firmware (issue #350).  Same passthrough policy: persist whatever
    # the device sent, including None / missing-key (no overwrite when
    # absent so a malformed STATUS doesn't blow away a known-good list).
    if "display_ports" in status and status["display_ports"] is not None:
        normalized_ports = _normalize_display_ports(status["display_ports"])
        if normalized_ports is not None:
            values["display_ports"] = normalized_ports

    result = await db.execute(
        update(Device)
        .where(Device.id == device_id)
        .where(
            (Device.last_status_ts.is_(None))
            | (Device.last_status_ts < ts)
        )
        .values(**values)
    )
    rowcount = result.rowcount or 0

    # Self-heal for the stale-presence sweep (PR #440): if a previous
    # leader-gated sweep marked this device offline (e.g., because the
    # Container Apps load balancer kept a dead WS connection "alive"
    # past the heartbeat threshold), accepting a new STATUS heartbeat
    # is positive evidence the device is alive — atomically flip
    # ``online`` back to ``true`` and dispatch the existing reconnect
    # path so any outstanding offline alert is cleared and a "back
    # online" notification fires when warranted.
    #
    # The CAS predicate ``online IS FALSE`` ensures only one replica
    # wins if heartbeats from multiple paths race; the loser sees
    # zero returned rows and skips dispatch.
    healed_row = None
    if rowcount > 0:
        heal_claim = await db.execute(
            update(Device)
            .where(Device.id == device_id)
            .where(Device.online.is_(False))
            .values(online=True)
            .returning(
                Device.name, Device.group_id, Device.status,
            )
        )
        healed_row = heal_claim.first()

    await db.commit()

    if healed_row is not None:
        # Look up group_name for the back-online notification payload.
        # Done after commit so we don't extend the heartbeat
        # transaction; tiny extra round-trip on the rare transition
        # edge only.
        group_name = ""
        if healed_row.group_id is not None:
            group_name = (
                await db.execute(
                    select(DeviceGroup.name).where(
                        DeviceGroup.id == healed_row.group_id
                    )
                )
            ).scalar_one_or_none() or ""
        try:
            from cms.services.alert_service import alert_service
            alert_service.device_reconnected(
                device_id,
                device_name=healed_row.name or device_id,
                group_id=str(healed_row.group_id) if healed_row.group_id else None,
                group_name=group_name,
                status=(
                    healed_row.status.value
                    if hasattr(healed_row.status, "value")
                    else str(healed_row.status)
                ),
            )
        except Exception:
            # Dispatch is fire-and-forget; never let an alert-service
            # failure break the heartbeat path.
            logger.exception(
                "Failed to dispatch reconnect for healed device %s", device_id,
            )

    return rowcount > 0


async def set_flags(
    db: AsyncSession, device_id: str, **flags: Any,
) -> None:
    """Optimistically update a subset of the toggle/flag columns.

    Used by the UI toggle endpoints (SSH, local-api) so the dashboard
    reflects the new value immediately, before the next STATUS
    heartbeat confirms it.  Unknown columns are ignored.
    """
    allowed = {"ssh_enabled", "local_api_enabled", "display_connected"}
    clean = {k: v for k, v in flags.items() if k in allowed}
    if not clean:
        return
    await db.execute(update(Device).where(Device.id == device_id).values(**clean))
    await db.commit()


async def is_online(db: AsyncSession, device_id: str) -> bool:
    """Return whether the device is currently marked online."""
    row = await db.execute(
        select(Device.online).where(Device.id == device_id)
    )
    val = row.scalar_one_or_none()
    return bool(val)


async def count_online(db: AsyncSession) -> int:
    """Return the number of devices currently marked online."""
    from sqlalchemy import func
    row = await db.execute(
        select(func.count()).select_from(Device).where(Device.online.is_(True))
    )
    return int(row.scalar_one() or 0)


async def ids_online(db: AsyncSession) -> list[str]:
    """Return the ids of every device currently marked online."""
    row = await db.execute(
        select(Device.id).where(Device.online.is_(True))
    )
    return [r[0] for r in row.all()]


async def list_states(db: AsyncSession) -> list[dict[str, Any]]:
    """Return the live state dict for every online device.

    Shape matches the old ``DeviceManager.get_all_states()`` contract —
    callers can swap over without touching their template/JSON code.
    Offline devices are filtered out so the dict is a drop-in for the
    "what's connected right now" view (UI, scheduler, dashboard).
    """
    row = await db.execute(
        select(*_STATE_COLUMNS).where(Device.online.is_(True))
    )
    return [_row_to_state(r) for r in row.all()]


async def list_states_for(
    db: AsyncSession, device_ids: Iterable[str],
) -> list[dict[str, Any]]:
    """Return live state for a specific subset of device ids (online or not).

    Used by endpoints that want telemetry for a device even when it's
    marked offline (e.g. the device detail page — the UI still wants to
    show the last-known temperature etc.).
    """
    ids = list(device_ids)
    if not ids:
        return []
    row = await db.execute(
        select(*_STATE_COLUMNS).where(Device.id.in_(ids))
    )
    return [_row_to_state(r) for r in row.all()]
