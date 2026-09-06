"""Group-scoped device tag API.

Tags label devices *within one group* so a schedule can target a subset of the
group without a second authorization boundary. Access is entirely derived from
the owning group: anyone who can read the group can read its tags, and anyone
who can write devices in it can manage them. That is deliberate — it is what
lets an Operator serve the multi-purpose-device case without needing the
user-administration rights that many-to-many groups would have required (#863).

Because tag identity is ``(group_id, lower(name))``, "Summer Promos" in Group A
and Group B are unrelated tags and no schedule can span both.
"""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import require_auth, require_permission, verify_resource_group_access
from cms.database import get_db
from cms.models.device import Device, DeviceGroup
from cms.models.device_tag import DeviceTag, DeviceTagAssignment
from cms.models.user import User
from cms.permissions import DEVICES_READ, DEVICES_WRITE
from cms.schemas.device_tag import (
    DeviceTagIn,
    DeviceTagOut,
    DeviceTagPatch,
    DeviceTagsOut,
    DeviceTagsUpdate,
)
from cms.services import device_tags as tag_service
from cms.services.audit_service import audit_log
from cms.services.device_config_validation import (
    DeviceTargeting,
    current_device_targeting,
    describe_conflicts,
    validate_device_transition,
)

router = APIRouter(prefix="/api", dependencies=[Depends(require_auth)])


def _out(tag: DeviceTag, group_name: str | None, device_count: int | None = None) -> DeviceTagOut:
    return DeviceTagOut(
        id=tag.id,
        group_id=tag.group_id,
        name=tag.name,
        color=tag.color,
        created_at=tag.created_at,
        group_name=group_name,
        qualified_name=tag_service.qualified_tag_name(group_name, tag.name),
        device_count=device_count,
    )


async def _load_group(db: AsyncSession, group_id: uuid.UUID) -> DeviceGroup:
    group = await db.get(DeviceGroup, group_id)
    if group is None:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


async def _load_tag(db: AsyncSession, tag_id: uuid.UUID) -> DeviceTag:
    tag = await tag_service.get_tag(db, tag_id)
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")
    return tag


async def _device_counts(db: AsyncSession, tag_ids: list[uuid.UUID]) -> dict:
    if not tag_ids:
        return {}
    rows = await db.execute(
        select(DeviceTagAssignment.tag_id, func.count())
        .where(DeviceTagAssignment.tag_id.in_(tag_ids))
        .group_by(DeviceTagAssignment.tag_id)
    )
    return dict(rows.all())


@router.get(
    "/groups/{group_id}/tags",
    response_model=List[DeviceTagOut],
    dependencies=[Depends(require_permission(DEVICES_READ))],
)
async def list_group_tags(
    group_id: uuid.UUID,
    user: User = Depends(require_permission(DEVICES_READ)),
    db: AsyncSession = Depends(get_db),
):
    group = await _load_group(db, group_id)
    await verify_resource_group_access(user, db, group_id)
    tags = await tag_service.list_group_tags(db, group_id)
    counts = await _device_counts(db, [t.id for t in tags])
    return [_out(t, group.name, counts.get(t.id, 0)) for t in tags]


@router.post(
    "/groups/{group_id}/tags",
    response_model=DeviceTagOut,
    status_code=201,
    dependencies=[Depends(require_permission(DEVICES_WRITE))],
)
async def create_group_tag(
    group_id: uuid.UUID,
    data: DeviceTagIn,
    request: Request,
    user: User = Depends(require_permission(DEVICES_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    group = await _load_group(db, group_id)
    await verify_resource_group_access(user, db, group_id)
    try:
        tag = await tag_service.create_tag(
            db,
            group_id=group_id,
            name=data.name,
            color=data.color,
            created_by_user_id=getattr(user, "id", None),
        )
    except tag_service.DeviceTagError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit_log(
        db,
        user=user,
        action="device_tag.create",
        resource_type="device_tag",
        resource_id=str(tag.id),
        description=f"Created device tag '{group.name}:{tag.name}'",
        details={"group_id": str(group_id), "name": tag.name},
        request=request,
    )
    await db.commit()
    return _out(tag, group.name, 0)


@router.patch(
    "/device-tags/{tag_id}",
    response_model=DeviceTagOut,
    dependencies=[Depends(require_permission(DEVICES_WRITE))],
)
async def update_group_tag(
    tag_id: uuid.UUID,
    data: DeviceTagPatch,
    request: Request,
    user: User = Depends(require_permission(DEVICES_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    tag = await _load_tag(db, tag_id)
    await verify_resource_group_access(user, db, tag.group_id)
    group = await _load_group(db, tag.group_id)
    try:
        if data.name is not None:
            await tag_service.rename_tag(db, tag, data.name)
        if data.color is not None:
            tag.color = tag_service.normalize_tag_color(data.color)
    except tag_service.DeviceTagError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    await audit_log(
        db,
        user=user,
        action="device_tag.update",
        resource_type="device_tag",
        resource_id=str(tag.id),
        description=f"Updated device tag '{group.name}:{tag.name}'",
        details={"group_id": str(tag.group_id), "name": tag.name},
        request=request,
    )
    await db.commit()
    counts = await _device_counts(db, [tag.id])
    return _out(tag, group.name, counts.get(tag.id, 0))


@router.delete(
    "/device-tags/{tag_id}",
    status_code=204,
    dependencies=[Depends(require_permission(DEVICES_WRITE))],
)
async def delete_group_tag(
    tag_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(DEVICES_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    tag = await _load_tag(db, tag_id)
    await verify_resource_group_access(user, db, tag.group_id)
    group = await _load_group(db, tag.group_id)
    name = tag.name
    await tag_service.delete_tag(db, tag)
    await audit_log(
        db,
        user=user,
        action="device_tag.delete",
        resource_type="device_tag",
        resource_id=str(tag_id),
        description=f"Deleted device tag '{group.name}:{name}'",
        details={"group_id": str(group.id), "name": name},
        request=request,
    )
    await db.commit()
    return None


@router.get(
    "/devices/{device_id}/tags",
    response_model=DeviceTagsOut,
    dependencies=[Depends(require_permission(DEVICES_READ))],
)
async def get_device_tags(
    device_id: str,
    user: User = Depends(require_permission(DEVICES_READ)),
    db: AsyncSession = Depends(get_db),
):
    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    await verify_resource_group_access(user, db, device.group_id)
    tags = await tag_service.get_device_tags(db, device_id)
    group = await db.get(DeviceGroup, device.group_id) if device.group_id else None
    group_name = group.name if group else None
    return DeviceTagsOut(
        device_id=device_id,
        group_id=device.group_id,
        tags=[_out(t, group_name) for t in tags],
    )


@router.put(
    "/devices/{device_id}/tags",
    response_model=DeviceTagsOut,
    dependencies=[Depends(require_permission(DEVICES_WRITE))],
)
async def set_device_tags(
    device_id: str,
    data: DeviceTagsUpdate,
    request: Request,
    user: User = Depends(require_permission(DEVICES_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Replace a device's tag set.

    Tagging changes which schedules are effective for the device, so the
    resulting configuration is validated before it is written: a change that
    would introduce an equal-priority overlap on this device is rejected rather
    than left for the runtime to resolve arbitrarily.
    """
    device = await db.get(Device, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")
    await verify_resource_group_access(user, db, device.group_id)

    before = await current_device_targeting(db, device_id)
    after = DeviceTargeting(
        group_id=device.group_id, tag_ids=frozenset(data.tag_ids)
    )
    validation = await validate_device_transition(
        db, after={device_id: after}, before={device_id: before}
    )
    result = validation.devices.get(device_id)
    if result is not None and result.introduced_conflicts:
        raise HTTPException(
            status_code=409,
            detail=(
                "These tags would make two equal-priority schedules overlap on "
                "this device: "
                + describe_conflicts(result.introduced_conflicts)
                + ". Change one schedule's priority or its tag first."
            ),
        )

    try:
        tags = await tag_service.set_device_tags(db, device, list(data.tag_ids))
    except tag_service.DeviceTagError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    group = await db.get(DeviceGroup, device.group_id) if device.group_id else None
    group_name = group.name if group else None
    await audit_log(
        db,
        user=user,
        action="device.tags_set",
        resource_type="device",
        resource_id=device_id,
        description=(
            f"Set tags on '{device.name or device_id}' to "
            + (", ".join(t.name for t in tags) or "(none)")
        ),
        details={
            "group_id": str(device.group_id) if device.group_id else None,
            "tags": [t.name for t in tags],
        },
        request=request,
    )
    await db.commit()

    from cms.services.scheduler import push_sync_to_device

    await push_sync_to_device(device_id, db)
    return DeviceTagsOut(
        device_id=device_id,
        group_id=device.group_id,
        tags=[_out(t, group_name) for t in tags],
    )
