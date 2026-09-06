"""Test helpers for assigning a device's owning group.

A device belongs to exactly one group, which is its authorization
boundary. These helpers keep the assignment out of individual tests so
the storage detail stays in one place.
"""

from __future__ import annotations

import uuid

from sqlalchemy import update

from cms.models.device import Device


async def assign_device_group(db, device_id: str, group_id: uuid.UUID | None) -> None:
    """Set ``device_id``'s owning group and flush."""
    await db.execute(
        update(Device).where(Device.id == device_id).values(group_id=group_id)
    )
    await db.flush()
