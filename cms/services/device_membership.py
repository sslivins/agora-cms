"""Manage a device's single owning group (``devices.group_id``).

A device belongs to exactly one group, and that group is the authorization
boundary: every schedule targeting the device belongs to it, so any schedule
conflict is always between schedules the same operators can read and fix.

Many-to-many device↔group *authority* was tried (#863) and reverted: group
access is granted through ``UserGroup`` rows behind ``users:write``, which
Operators do not hold, so an Operator who put a shared device into a second
group could neither be told about the resulting conflict (the other group's
schedules sit outside their read scope) nor fix it.

The multi-purpose-device use case that motivated many-to-many is served by
group-scoped tags, which narrow a schedule to a subset of its owning group's
devices and carry no access control of their own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Iterable

from sqlalchemy import false, func, select

from cms.models.device import Device, DeviceStatus
from cms.models.device_tag import DeviceTag, DeviceTagAssignment


@dataclass(slots=True)
class DeviceGroupChange:
    """The before/after of a device's owning-group assignment."""

    previous_group_id: uuid.UUID | None
    new_group_id: uuid.UUID | None
    changed: bool
    dropped_tags: list[str] = field(default_factory=list)


def effective_device_group_rows_subquery(
    *,
    device_ids: Iterable[str] | None = None,
    group_ids: Iterable[uuid.UUID] | None = None,
    statuses: DeviceStatus | Iterable[DeviceStatus] | None = None,
):
    """Return ``(device_id, group_id)`` rows for grouped devices.

    Kept as the single seam every caller resolves devices through, so schedule
    targeting, RBAC scoping and the UI all agree on what "the devices of a
    group" means. Ungrouped devices are excluded — they are not a target.
    """
    rows = select(
        Device.id.label("device_id"),
        Device.group_id.label("group_id"),
    ).where(Device.group_id.is_not(None))

    device_id_list = list(device_ids) if device_ids is not None else None
    if device_id_list is not None:
        rows = (
            rows.where(Device.id.in_(device_id_list))
            if device_id_list
            else rows.where(false())
        )

    group_id_list = list(group_ids) if group_ids is not None else None
    if group_id_list is not None:
        rows = (
            rows.where(Device.group_id.in_(group_id_list))
            if group_id_list
            else rows.where(false())
        )

    if statuses is not None:
        status_list = (
            [statuses] if isinstance(statuses, DeviceStatus) else list(statuses)
        )
        rows = (
            rows.where(Device.status.in_(status_list))
            if status_list
            else rows.where(false())
        )

    return rows.subquery()


async def set_device_group(
    db,
    device: Device,
    group_id: uuid.UUID | None,
    *,
    dry_run: bool = False,
) -> DeviceGroupChange:
    """Assign ``device``'s owning group. Idempotent; does not commit.

    Tags are scoped to the group that defines them, so a device leaving a group
    leaves that group's tags behind rather than carrying meaningless labels
    into the new one. The dropped tag names are returned so callers can tell
    the operator what was lost instead of having it happen silently.
    """
    from cms.services.device_tags import clear_device_tags

    previous = device.group_id
    change = DeviceGroupChange(
        previous_group_id=previous,
        new_group_id=group_id,
        changed=(previous != group_id),
    )
    if not change.changed:
        return change

    if dry_run:
        result = await db.execute(
            select(DeviceTag.name)
            .join(DeviceTagAssignment, DeviceTagAssignment.tag_id == DeviceTag.id)
            .where(DeviceTagAssignment.device_id == device.id)
            .order_by(func.lower(DeviceTag.name))
        )
        change.dropped_tags = list(result.scalars().all())
        return change

    change.dropped_tags = await clear_device_tags(db, device.id)
    device.group_id = group_id
    return change
