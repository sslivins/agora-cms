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

from cms.models.device import Device

logger = logging.getLogger("agora.cms.device_presence")


# Columns returned by :func:`list_states` — matches the keys the old
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
    Device.connection_id,
    Device.online,
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
        # Not a column — retained as ``None`` for API shape compatibility.
        # IP address is a per-connection property that doesn't survive
        # across replicas; callers that care use the direct-WS path.
        "ip_address": None,
        "error": row.error,
        "error_since": error_since.isoformat() if error_since else None,
        "ssh_enabled": row.ssh_enabled,
        "local_api_enabled": row.local_api_enabled,
        "display_connected": row.display_connected,
        "connection_id": row.connection_id,
        "online": bool(row.online),
    }


async def mark_online(
    db: AsyncSession,
    device_id: str,
    connection_id: str | None = None,
) -> None:
    """Flip ``devices.online`` to ``true`` for *device_id*.

    Also refreshes ``last_seen`` so the "last heard from" display on
    the UI is immediately accurate after reconnect.  ``connection_id``
    is persisted when the caller has one (WPS webhook path); direct-WS
    connections don't expose a stable id and pass ``None``.
    """
    now = datetime.now(timezone.utc)
    await db.execute(
        update(Device)
        .where(Device.id == device_id)
        .values(online=True, connection_id=connection_id, last_seen=now)
    )
    await db.commit()


async def mark_offline(db: AsyncSession, device_id: str) -> None:
    """Flip ``devices.online`` to ``false`` and clear ``connection_id``."""
    await db.execute(
        update(Device)
        .where(Device.id == device_id)
        .values(online=False, connection_id=None)
    )
    await db.commit()


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

    result = await db.execute(
        update(Device)
        .where(Device.id == device_id)
        .where(
            (Device.last_status_ts.is_(None))
            | (Device.last_status_ts < ts)
        )
        .values(**values)
    )
    await db.commit()
    return (result.rowcount or 0) > 0


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
