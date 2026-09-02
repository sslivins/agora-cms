"""Manage device↔group memberships during the group-id coexistence window.

Stage 2 introduced ``device_group_memberships`` as an exact mirror of the
legacy scalar ``devices.group_id`` column.  That legacy mirror helper,
``set_single_group_membership()``, must remain untouched because older write
paths still rely on its "exactly one membership" contract.

Stage 6 adds true multi-membership writers alongside it.  These helpers update
the join table with add/remove/replace semantics while also keeping the legacy
scalar on the ``Device`` row pinned to one member of the resulting set for
backward compatibility.  The scalar remains the deprecated single-group view;
the join table carries the full membership set.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.device_group_membership import DeviceGroupMembership


@dataclass(slots=True)
class DeviceMembershipChange:
    current_group_ids: tuple[uuid.UUID, ...]
    result_group_ids: tuple[uuid.UUID, ...]
    added_group_ids: tuple[uuid.UUID, ...]
    removed_group_ids: tuple[uuid.UUID, ...]
    legacy_group_id: uuid.UUID | None
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
    ordered: list[uuid.UUID] = []
    scalar_group_id = getattr(device, "group_id", None)
    if scalar_group_id is not None:
        ordered.append(scalar_group_id)
        current.discard(scalar_group_id)
    ordered.extend(sorted(current, key=lambda gid: str(gid)))
    return ordered


def _choose_legacy_group_id(
    *,
    current_group_id: uuid.UUID | None,
    desired_group_ids: list[uuid.UUID],
) -> uuid.UUID | None:
    """Select the scalar ``devices.group_id`` representative.

    Prefer to preserve the current scalar when it still belongs to the new set;
    otherwise choose the first requested group.  If the caller did not provide a
    stable order (e.g. remove-one operations), the wrapper should pass the
    desired groups in its preferred deterministic order.
    """
    if current_group_id is not None and current_group_id in desired_group_ids:
        return current_group_id
    return desired_group_ids[0] if desired_group_ids else None


async def _plan_membership_change(
    db: AsyncSession,
    device,
    desired_group_ids: Iterable[uuid.UUID] | None,
) -> tuple[DeviceMembershipChange, list[uuid.UUID]]:
    ordered_desired = _unique_group_ids(desired_group_ids)
    current = await _load_membership_group_ids(db, device.id)
    if getattr(device, "group_id", None) is not None:
        current.add(device.group_id)
    desired = set(ordered_desired)
    added = desired - current
    removed = current - desired
    return (
        DeviceMembershipChange(
            current_group_ids=_sorted_group_ids(current),
            result_group_ids=_sorted_group_ids(desired),
            added_group_ids=_sorted_group_ids(added),
            removed_group_ids=_sorted_group_ids(removed),
            legacy_group_id=_choose_legacy_group_id(
                current_group_id=getattr(device, "group_id", None),
                desired_group_ids=ordered_desired,
            ),
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
    one if it isn't already present. Does not commit — the caller owns the
    transaction so the membership write lands atomically with the ``group_id``
    change it mirrors.
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

    During Stage 6 coexistence the join table carries the complete set, while
    ``device.group_id`` keeps a single representative for backward-compatible
    readers.  No commit is performed here.
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

    device.group_id = change.legacy_group_id
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
