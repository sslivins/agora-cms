"""Manage device↔group memberships in ``device_group_memberships``."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import delete, false, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.device import Device, DeviceStatus
from cms.models.device_group_membership import DeviceGroupMembership


@dataclass(slots=True)
class DeviceMembershipChange:
    current_group_ids: tuple[uuid.UUID, ...]
    result_group_ids: tuple[uuid.UUID, ...]
    added_group_ids: tuple[uuid.UUID, ...]
    removed_group_ids: tuple[uuid.UUID, ...]
    changed: bool


def _unique_group_ids(group_ids: Iterable[uuid.UUID] | None) -> list[uuid.UUID]:
    seen: set[uuid.UUID] = set()
    ordered: list[uuid.UUID] = []
    for raw in group_ids or ():
        if raw in seen:
            continue
        seen.add(raw)
        ordered.append(raw)
    return ordered


def _sorted_group_ids(group_ids: Iterable[uuid.UUID]) -> tuple[uuid.UUID, ...]:
    return tuple(sorted(group_ids, key=lambda gid: str(gid)))


def effective_device_group_rows_subquery(
    *,
    device_ids: Iterable[str] | None = None,
    group_ids: Iterable[uuid.UUID] | None = None,
    statuses: DeviceStatus | Iterable[DeviceStatus] | None = None,
):
    """Return ``(device_id, group_id)`` rows for the effective device groups.

    Stage 8b removes the legacy scalar ``devices.group_id`` column, so the join
    table is now the sole source of truth.
    """
    rows = select(
        DeviceGroupMembership.device_id.label("device_id"),
        DeviceGroupMembership.group_id.label("group_id"),
    )

    device_id_list = list(device_ids) if device_ids is not None else None
    if device_id_list is not None:
        if device_id_list:
            rows = rows.where(
                DeviceGroupMembership.device_id.in_(device_id_list)
            )
        else:
            rows = rows.where(false())

    group_id_list = list(group_ids) if group_ids is not None else None
    if group_id_list is not None:
        if group_id_list:
            rows = rows.where(
                DeviceGroupMembership.group_id.in_(group_id_list)
            )
        else:
            rows = rows.where(false())

    if statuses is not None:
        status_list = (
            [statuses]
            if isinstance(statuses, DeviceStatus)
            else list(statuses)
        )
        if status_list:
            rows = (
                rows
                .join(Device, Device.id == DeviceGroupMembership.device_id)
                .where(Device.status.in_(status_list))
            )
        else:
            rows = rows.where(false())

    return rows.distinct().subquery()


async def _load_membership_group_ids(
    db: AsyncSession,
    device_id: str,
) -> set[uuid.UUID]:
    result = await db.execute(
        select(DeviceGroupMembership.group_id).where(
            DeviceGroupMembership.device_id == device_id
        )
    )
    return set(result.scalars().all())


async def _current_effective_group_ids_in_preferred_order(
    db: AsyncSession,
    device,
) -> list[uuid.UUID]:
    current = await _load_membership_group_ids(db, device.id)
    return sorted(current, key=lambda gid: str(gid))


async def _plan_membership_change(
    db: AsyncSession,
    device,
    desired_group_ids: Iterable[uuid.UUID] | None,
) -> tuple[DeviceMembershipChange, list[uuid.UUID]]:
    ordered_desired = _unique_group_ids(desired_group_ids)
    current = await _load_membership_group_ids(db, device.id)
    desired = set(ordered_desired)
    added = desired - current
    removed = current - desired
    return (
        DeviceMembershipChange(
            current_group_ids=_sorted_group_ids(current),
            result_group_ids=_sorted_group_ids(desired),
            added_group_ids=_sorted_group_ids(added),
            removed_group_ids=_sorted_group_ids(removed),
            changed=(current != desired),
        ),
        ordered_desired,
    )


async def set_single_group_membership(
    db: AsyncSession,
    device_id: str,
    group_id: uuid.UUID | None,
) -> None:
    """Make ``device_id``'s membership set exactly ``{group_id}`` (or empty).

    Idempotent: deletes any memberships that don't match and inserts the target
    one if it isn't already present. Does not commit.
    """
    stmt = delete(DeviceGroupMembership).where(
        DeviceGroupMembership.device_id == device_id
    )
    if group_id is not None:
        stmt = stmt.where(DeviceGroupMembership.group_id != group_id)
    await db.execute(stmt)

    if group_id is None:
        return

    existing = await db.execute(
        select(DeviceGroupMembership).where(
            DeviceGroupMembership.device_id == device_id,
            DeviceGroupMembership.group_id == group_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(DeviceGroupMembership(device_id=device_id, group_id=group_id))


async def replace_device_group_memberships(
    db: AsyncSession,
    device,
    group_ids: Iterable[uuid.UUID] | None,
    *,
    dry_run: bool = False,
) -> DeviceMembershipChange:
    """Replace a device's full membership set with ``group_ids``.

    The join table is the source of truth. No commit is performed here.
    """
    change, ordered_desired = await _plan_membership_change(db, device, group_ids)
    if dry_run or not change.changed:
        return change

    stmt = delete(DeviceGroupMembership).where(
        DeviceGroupMembership.device_id == device.id
    )
    if ordered_desired:
        stmt = stmt.where(DeviceGroupMembership.group_id.not_in(ordered_desired))
    await db.execute(stmt)

    existing = await _load_membership_group_ids(db, device.id)
    for group_id in ordered_desired:
        if group_id not in existing:
            db.add(DeviceGroupMembership(device_id=device.id, group_id=group_id))
    return change


async def add_device_to_group(
    db: AsyncSession,
    device,
    group_id: uuid.UUID,
    *,
    dry_run: bool = False,
) -> DeviceMembershipChange:
    """Idempotently add ``device`` to ``group_id`` without disturbing others."""
    ordered_desired = await _current_effective_group_ids_in_preferred_order(db, device)
    ordered_desired.append(group_id)
    return await replace_device_group_memberships(
        db,
        device,
        ordered_desired,
        dry_run=dry_run,
    )


async def remove_device_from_group(
    db: AsyncSession,
    device,
    group_id: uuid.UUID,
    *,
    dry_run: bool = False,
) -> DeviceMembershipChange:
    """Idempotently remove ``group_id`` from ``device``'s membership set."""
    desired = [
        existing_group_id
        for existing_group_id in await _current_effective_group_ids_in_preferred_order(db, device)
        if existing_group_id != group_id
    ]
    return await replace_device_group_memberships(
        db,
        device,
        desired,
        dry_run=dry_run,
    )
