"""Tag CRUD API.

Tags are an admin-managed, org-flat vocabulary that any
``assets:write`` user can apply to assets via ``POST /api/assets/bulk``
(``add_tag`` / ``remove_tag``).  Creating, renaming, recoloring, or
deleting a tag is admin-only -- the asset library treats the tag set
as shared org metadata, not user-private.
"""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_user_group_ids, require_auth, require_permission
from cms.database import get_db
from cms.models.tag import AssetTag, DEFAULT_TAG_COLOR, Tag
from cms.models.user import User
from cms.permissions import ASSETS_READ, ASSETS_WRITE
from cms.schemas.tag import TagIn, TagOut, TagPatch
from cms.services.audit_service import audit_log


router = APIRouter(prefix="/api/tags", dependencies=[Depends(require_auth)])


async def _require_admin(user: User, db: AsyncSession) -> None:
    """Raise 403 if the user is scoped to groups (= non-admin)."""
    if await get_user_group_ids(user, db) is not None:
        raise HTTPException(
            status_code=403,
            detail="Tag management is admin-only",
        )


async def _existing_by_name(db: AsyncSession, name: str) -> Tag | None:
    """Look up a tag by canonical (lowercased) name.

    The model already enforces case-insensitive uniqueness via the
    functional index, but the explicit query lets the API return a
    friendlier 400 instead of relying on a DB IntegrityError.
    """
    return (
        await db.execute(
            select(Tag).where(func.lower(Tag.name) == name.lower())
        )
    ).scalar_one_or_none()


@router.get("", response_model=List[TagOut], dependencies=[Depends(require_permission(ASSETS_READ))])
async def list_tags(
    user: User = Depends(require_permission(ASSETS_READ)),
    db: AsyncSession = Depends(get_db),
):
    """List all tags, with per-tag asset counts."""
    rows = (
        await db.execute(
            select(Tag, func.count(AssetTag.id))
            .outerjoin(AssetTag, AssetTag.tag_id == Tag.id)
            .group_by(Tag.id)
            .order_by(Tag.name)
        )
    ).all()
    out: list[TagOut] = []
    for tag, count in rows:
        t = TagOut.model_validate(tag)
        t.asset_count = int(count or 0)
        out.append(t)
    return out


@router.post("", response_model=TagOut, status_code=201, dependencies=[Depends(require_permission(ASSETS_WRITE))])
async def create_tag(
    payload: TagIn,
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Create a new tag (admin only)."""
    await _require_admin(user, db)

    if await _existing_by_name(db, payload.name):
        raise HTTPException(status_code=400, detail=f"Tag {payload.name!r} already exists")

    tag = Tag(
        name=payload.name,
        color=payload.color or DEFAULT_TAG_COLOR,
        created_by_user_id=user.id,
    )
    db.add(tag)
    await db.flush()

    await audit_log(
        db, user=user, action="tag.create", resource_type="tag",
        resource_id=str(tag.id),
        description=f"Created tag '{tag.name}'",
        details={"tag_name": tag.name, "tag_color": tag.color},
        request=request,
    )
    await db.commit()
    await db.refresh(tag)
    return TagOut.model_validate(tag)


@router.patch("/{tag_id}", response_model=TagOut, dependencies=[Depends(require_permission(ASSETS_WRITE))])
async def update_tag(
    tag_id: uuid.UUID,
    payload: TagPatch,
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Rename / recolor a tag (admin only)."""
    await _require_admin(user, db)

    tag = (await db.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    changes: dict[str, tuple[str, str]] = {}
    if payload.name is not None and payload.name != tag.name:
        clash = await _existing_by_name(db, payload.name)
        if clash and clash.id != tag.id:
            raise HTTPException(
                status_code=400,
                detail=f"Tag {payload.name!r} already exists",
            )
        changes["name"] = (tag.name, payload.name)
        tag.name = payload.name
    if payload.color is not None and payload.color != tag.color:
        changes["color"] = (tag.color, payload.color)
        tag.color = payload.color

    if changes:
        await audit_log(
            db, user=user, action="tag.update", resource_type="tag",
            resource_id=str(tag.id),
            description=f"Updated tag '{tag.name}'",
            details={"changes": {k: {"from": v[0], "to": v[1]} for k, v in changes.items()}},
            request=request,
        )
    await db.commit()
    await db.refresh(tag)
    return TagOut.model_validate(tag)


@router.delete("/{tag_id}", status_code=204, dependencies=[Depends(require_permission(ASSETS_WRITE))])
async def delete_tag(
    tag_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(ASSETS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete a tag.  Junction rows cascade.  Admin only."""
    await _require_admin(user, db)

    tag = (await db.execute(select(Tag).where(Tag.id == tag_id))).scalar_one_or_none()
    if not tag:
        raise HTTPException(status_code=404, detail="Tag not found")

    # Capture for the audit entry before the row goes away.
    tag_name = tag.name
    asset_count = int(
        (await db.execute(
            select(func.count(AssetTag.id)).where(AssetTag.tag_id == tag_id)
        )).scalar() or 0
    )

    await db.execute(delete(Tag).where(Tag.id == tag_id))
    await audit_log(
        db, user=user, action="tag.delete", resource_type="tag",
        resource_id=str(tag_id),
        description=f"Deleted tag '{tag_name}'",
        details={"tag_name": tag_name, "removed_from_assets": asset_count},
        request=request,
    )
    await db.commit()


@router.get(
    "/{tag_id}/members",
    dependencies=[Depends(require_permission(ASSETS_READ))],
)
async def list_tag_members(
    tag_id: uuid.UUID,
    user: User = Depends(require_permission(ASSETS_READ)),
    db: AsyncSession = Depends(get_db),
):
    """Return a tag's current member assets in device-resolve order.

    Powers the slideshow builder's collapsible tag-block preview. The
    members are exactly what the slideshow resolver would expand this tag
    into — eligible asset types only, ordered ``tagged_at`` ascending —
    intersected with the caller's visible-asset ACL and enriched with each
    asset's ready thumbnail URL. Membership is dynamic, so this is a
    point-in-time snapshot, not a stored ordering.

    Reuses :func:`cms.services.slideshow_resolver.expand_tag_members` so the
    preview can't drift from the order the device actually plays.
    """
    from cms.models.asset import Asset
    from cms.routers.assets import _thumbnail_urls_for, _visible_asset_ids
    from cms.services.slideshow_resolver import expand_tag_members

    tag = (
        await db.execute(select(Tag).where(Tag.id == tag_id))
    ).scalar_one_or_none()
    if tag is None:
        raise HTTPException(status_code=404, detail="Tag not found")

    member_ids = await expand_tag_members(tag_id, db)

    # Intersect with the caller's visible set (None = unrestricted/admin),
    # preserving resolve order.
    visible = await _visible_asset_ids(user, db)
    if visible is not None:
        allowed = set(visible)
        member_ids = [aid for aid in member_ids if aid in allowed]

    total = len(member_ids)
    thumbs = await _thumbnail_urls_for(member_ids, db) if member_ids else {}

    detail: dict[uuid.UUID, dict] = {}
    if member_ids:
        rows = (
            await db.execute(
                select(
                    Asset.id,
                    Asset.asset_type,
                    Asset.display_name,
                    Asset.original_filename,
                    Asset.filename,
                    Asset.duration_seconds,
                ).where(Asset.id.in_(member_ids))
            )
        ).all()
        for aid, atype, dname, ofn, fn, dur in rows:
            type_str = atype.value if hasattr(atype, "value") else str(atype)
            detail[aid] = {
                "id": str(aid),
                "asset_type": type_str,
                "name": dname or ofn or fn or str(aid),
                "thumbnail_url": thumbs.get(aid),
                "duration_seconds": dur,
            }

    members = [detail[aid] for aid in member_ids if aid in detail]
    return {"tag_id": str(tag_id), "total": total, "members": members}
