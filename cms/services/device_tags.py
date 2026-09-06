"""Group-scoped device tags.

Tags narrow a schedule from "every device in the group" to "every device in the
group carrying this tag". They are the replacement for many-to-many device↔group
authority (#863): a label carries no access control of its own, and because tag
identity is ``(group_id, lower(name))`` there is no way to express a target that
spans groups. The owning group therefore remains the sole authorization
boundary, and every schedule conflict stays inside one group's read scope.

Authorization is entirely derived: callers check access to the *group* (via
``verify_resource_group_access``) and everything scoped to it follows.
"""

from __future__ import annotations

import re
import uuid
from typing import Iterable, Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload

from cms.models.device import Device
from cms.models.device_tag import (
    DEFAULT_DEVICE_TAG_COLOR,
    DeviceTag,
    DeviceTagAssignment,
)

MAX_TAG_NAME_LENGTH = 64
_WHITESPACE_RUN = re.compile(r"\s+")
_HEX_COLOR = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


class DeviceTagError(ValueError):
    """Raised for caller-correctable tag problems (bad name, wrong group)."""


def normalize_tag_name(name: str) -> str:
    """Trim, collapse internal whitespace, and validate a tag name."""
    cleaned = _WHITESPACE_RUN.sub(" ", (name or "").strip())
    if not cleaned:
        raise DeviceTagError("Tag name cannot be empty")
    if len(cleaned) > MAX_TAG_NAME_LENGTH:
        raise DeviceTagError(
            f"Tag name cannot exceed {MAX_TAG_NAME_LENGTH} characters"
        )
    return cleaned


def normalize_tag_color(color: str | None) -> str:
    if color is None or not color.strip():
        return DEFAULT_DEVICE_TAG_COLOR
    cleaned = color.strip()
    if not _HEX_COLOR.match(cleaned):
        raise DeviceTagError("Tag color must be a hex value like #737373")
    return cleaned.lower()


def qualified_tag_name(group_name: str | None, tag_name: str) -> str:
    """The ``<Group>:<Tag>`` display form used by the scheduler target picker."""
    return f"{group_name or '?'}:{tag_name}"


async def list_group_tags(db, group_id: uuid.UUID) -> list[DeviceTag]:
    result = await db.execute(
        select(DeviceTag)
        .where(DeviceTag.group_id == group_id)
        .order_by(func.lower(DeviceTag.name))
    )
    return list(result.scalars().all())


async def list_tags_for_groups(
    db, group_ids: Iterable[uuid.UUID]
) -> list[DeviceTag]:
    ids = list(group_ids)
    if not ids:
        return []
    result = await db.execute(
        select(DeviceTag)
        .options(selectinload(DeviceTag.group))
        .where(DeviceTag.group_id.in_(ids))
        .order_by(func.lower(DeviceTag.name))
    )
    return list(result.scalars().all())


async def get_tag(db, tag_id: uuid.UUID) -> DeviceTag | None:
    result = await db.execute(select(DeviceTag).where(DeviceTag.id == tag_id))
    return result.scalar_one_or_none()


async def _find_by_name(db, group_id: uuid.UUID, name: str) -> DeviceTag | None:
    result = await db.execute(
        select(DeviceTag).where(
            DeviceTag.group_id == group_id,
            func.lower(DeviceTag.name) == name.lower(),
        )
    )
    return result.scalar_one_or_none()


async def create_tag(
    db,
    *,
    group_id: uuid.UUID,
    name: str,
    color: str | None = None,
    created_by_user_id: uuid.UUID | None = None,
) -> DeviceTag:
    """Create a tag in ``group_id``. Does not commit."""
    clean_name = normalize_tag_name(name)
    clean_color = normalize_tag_color(color)
    if await _find_by_name(db, group_id, clean_name) is not None:
        raise DeviceTagError(
            f"A tag named '{clean_name}' already exists in this group"
        )
    tag = DeviceTag(
        group_id=group_id,
        name=clean_name,
        color=clean_color,
        created_by_user_id=created_by_user_id,
    )
    db.add(tag)
    await db.flush()
    return tag


async def rename_tag(db, tag: DeviceTag, name: str) -> DeviceTag:
    clean_name = normalize_tag_name(name)
    if clean_name.lower() != tag.name.lower():
        existing = await _find_by_name(db, tag.group_id, clean_name)
        if existing is not None:
            raise DeviceTagError(
                f"A tag named '{clean_name}' already exists in this group"
            )
    tag.name = clean_name
    await db.flush()
    return tag


async def delete_tag(db, tag: DeviceTag) -> None:
    """Delete a tag. Assignments and any schedule targeting it cascade away."""
    await db.delete(tag)
    await db.flush()


async def get_device_tags(db, device_id: str) -> list[DeviceTag]:
    result = await db.execute(
        select(DeviceTag)
        .join(DeviceTagAssignment, DeviceTagAssignment.tag_id == DeviceTag.id)
        .where(DeviceTagAssignment.device_id == device_id)
        .order_by(func.lower(DeviceTag.name))
    )
    return list(result.scalars().all())


async def get_tags_by_device_ids(
    db, device_ids: Iterable[str]
) -> dict[str, list[DeviceTag]]:
    ids = list(device_ids)
    if not ids:
        return {}
    result = await db.execute(
        select(DeviceTagAssignment.device_id, DeviceTag)
        .join(DeviceTag, DeviceTagAssignment.tag_id == DeviceTag.id)
        .where(DeviceTagAssignment.device_id.in_(ids))
        .order_by(func.lower(DeviceTag.name))
    )
    out: dict[str, list[DeviceTag]] = {}
    for device_id, tag in result.all():
        out.setdefault(device_id, []).append(tag)
    return out


async def set_device_tags(
    db, device: Device, tag_ids: Sequence[uuid.UUID]
) -> list[DeviceTag]:
    """Replace ``device``'s tags. Every tag must belong to the device's group.

    Rejecting cross-group tags here is what keeps the group the only
    authorization boundary — a device can never be labelled with a tag that
    somebody else's group defines.
    """
    wanted = list(dict.fromkeys(tag_ids))
    if wanted and device.group_id is None:
        raise DeviceTagError("Assign the device to a group before tagging it")

    tags: list[DeviceTag] = []
    if wanted:
        result = await db.execute(select(DeviceTag).where(DeviceTag.id.in_(wanted)))
        tags = list(result.scalars().all())
        found = {tag.id for tag in tags}
        missing = [str(tid) for tid in wanted if tid not in found]
        if missing:
            raise DeviceTagError(f"Unknown tag(s): {', '.join(missing)}")
        foreign = [tag.name for tag in tags if tag.group_id != device.group_id]
        if foreign:
            raise DeviceTagError(
                "Tag(s) belong to a different group: " + ", ".join(sorted(foreign))
            )

    await db.execute(
        delete(DeviceTagAssignment).where(
            DeviceTagAssignment.device_id == device.id
        )
    )
    for tag in tags:
        db.add(DeviceTagAssignment(device_id=device.id, tag_id=tag.id))
    await db.flush()
    return sorted(tags, key=lambda t: t.name.lower())


async def clear_device_tags(db, device_id: str) -> list[str]:
    """Drop every tag assignment for a device; returns the dropped tag names.

    Called when a device changes group: tags are defined by the group, so they
    do not travel with the device.
    """
    result = await db.execute(
        select(DeviceTag.name)
        .join(DeviceTagAssignment, DeviceTagAssignment.tag_id == DeviceTag.id)
        .where(DeviceTagAssignment.device_id == device_id)
        .order_by(func.lower(DeviceTag.name))
    )
    dropped = list(result.scalars().all())
    if dropped:
        await db.execute(
            delete(DeviceTagAssignment).where(
                DeviceTagAssignment.device_id == device_id
            )
        )
        await db.flush()
    return dropped


async def tagged_device_ids(db, tag_id: uuid.UUID) -> set[str]:
    result = await db.execute(
        select(DeviceTagAssignment.device_id).where(
            DeviceTagAssignment.tag_id == tag_id
        )
    )
    return set(result.scalars().all())


def tagged_device_ids_subquery(tag_id: uuid.UUID):
    """``device_id`` rows carrying ``tag_id``, for composing into larger queries."""
    return (
        select(DeviceTagAssignment.device_id.label("device_id"))
        .where(DeviceTagAssignment.tag_id == tag_id)
        .subquery()
    )
