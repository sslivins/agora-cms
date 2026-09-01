"""Keep ``device_group_memberships`` in sync with ``devices.group_id``.

Stage 2 of the device-groups many-to-many rework (#863). While the CMS is in
the expand/contract coexistence window, the new join table is written as an
exact mirror of the legacy single ``group_id`` FK. Every production site that
mutates ``Device.group_id`` calls :func:`set_single_group_membership` so the
two representations can never diverge.

At-most-one membership is enforced here on purpose: true multi-membership is
only enabled once every replica reads memberships instead of ``group_id``
(a later stage). Until then the join table is a faithful mirror.
"""

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.device_group_membership import DeviceGroupMembership


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
    # Drop every membership that isn't the target group.
    stmt = delete(DeviceGroupMembership).where(
        DeviceGroupMembership.device_id == device_id
    )
    if group_id is not None:
        stmt = stmt.where(DeviceGroupMembership.group_id != group_id)
    await db.execute(stmt)

    if group_id is None:
        return

    # Insert the target membership if it isn't already there.
    existing = await db.execute(
        select(DeviceGroupMembership).where(
            DeviceGroupMembership.device_id == device_id,
            DeviceGroupMembership.group_id == group_id,
        )
    )
    if existing.scalar_one_or_none() is None:
        db.add(DeviceGroupMembership(device_id=device_id, group_id=group_id))
