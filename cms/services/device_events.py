"""Shared device-event emission helpers."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.device import DeviceGroup
from cms.models.device_event import DeviceEvent
from cms.models.device_group_membership import DeviceGroupMembership


def _coerce_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None:
        return None
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def snapshot_device_groups(
    db: AsyncSession,
    *,
    device_id: str,
    primary_group_id: uuid.UUID | str | None = None,
    primary_group_name: str | None = None,
) -> list[dict[str, str]]:
    """Snapshot a device's full effective group set inside the current txn."""
    rows = (
        await db.execute(
            select(DeviceGroup.id, DeviceGroup.name)
            .join(
                DeviceGroupMembership,
                DeviceGroupMembership.group_id == DeviceGroup.id,
            )
            .where(DeviceGroupMembership.device_id == device_id)
        )
    ).all()

    ordered: list[tuple[uuid.UUID, str]] = []
    seen: set[uuid.UUID] = set()

    primary_uuid = _coerce_uuid(primary_group_id)
    if primary_uuid is not None:
        primary_name_value = primary_group_name
        if not primary_name_value:
            primary_name_value = await db.scalar(
                select(DeviceGroup.name).where(DeviceGroup.id == primary_uuid)
            )
        ordered.append((primary_uuid, primary_name_value or ""))
        seen.add(primary_uuid)

    for gid, name in sorted(rows, key=lambda row: (str(row[0]), row[1] or "")):
        if gid in seen:
            continue
        ordered.append((gid, name or ""))
        seen.add(gid)

    return [{"id": str(gid), "name": name} for gid, name in ordered]


async def emit_device_event(
    db: AsyncSession,
    *,
    event_type: str,
    device_id: str | None = None,
    device_name: str = "",
    details: dict | None = None,
    created_at: datetime | None = None,
    primary_group_id: uuid.UUID | str | None = None,
    primary_group_name: str | None = None,
    group_snapshot: list[dict[str, str]] | None = None,
) -> DeviceEvent:
    """Create a ``DeviceEvent`` with a frozen membership snapshot.

    Caller owns the transaction; this helper only stages the ORM row.
    """
    if created_at is None:
        created_at = datetime.now(timezone.utc)
    if device_id is None:
        snapshot = []
    else:
        snapshot = group_snapshot or await snapshot_device_groups(
            db,
            device_id=device_id,
            primary_group_id=primary_group_id,
            primary_group_name=primary_group_name,
        )

    legacy_group_id = _coerce_uuid(snapshot[0]["id"]) if snapshot else _coerce_uuid(primary_group_id)
    legacy_group_name = snapshot[0]["name"] if snapshot else (primary_group_name or "")

    event = DeviceEvent(
        id=uuid.uuid4(),
        device_id=device_id,
        device_name=device_name,
        group_id=legacy_group_id,
        group_name=legacy_group_name,
        group_ids=[row["id"] for row in snapshot],
        group_snapshots=snapshot,
        event_type=event_type,
        details=details,
        created_at=created_at,
    )
    db.add(event)
    return event
