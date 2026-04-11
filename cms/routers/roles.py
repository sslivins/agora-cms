"""Role management API routes."""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import require_permission
from cms.database import get_db
from cms.models.user import Role, User
from cms.permissions import ALL_PERMISSIONS, ROLES_READ, ROLES_WRITE
from cms.schemas.user import RoleCreate, RoleRead, RoleUpdate

router = APIRouter(prefix="/api/roles")


@router.get("", response_model=list[RoleRead])
async def list_roles(
    _user: User = Depends(require_permission(ROLES_READ)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Role).order_by(Role.name))
    return result.scalars().all()


@router.get("/{role_id}", response_model=RoleRead)
async def get_role(
    role_id: uuid.UUID,
    _user: User = Depends(require_permission(ROLES_READ)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


@router.post("", response_model=RoleRead, status_code=201)
async def create_role(
    data: RoleCreate,
    _user: User = Depends(require_permission(ROLES_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    # Validate permissions
    invalid = set(data.permissions) - set(ALL_PERMISSIONS)
    if invalid:
        raise HTTPException(status_code=422, detail=f"Invalid permissions: {sorted(invalid)}")

    # Check name uniqueness
    exists = await db.execute(select(Role).where(Role.name == data.name))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Role name already exists")

    role = Role(
        name=data.name,
        description=data.description,
        permissions=data.permissions,
        is_builtin=False,
    )
    db.add(role)
    await db.commit()
    await db.refresh(role)
    return role


@router.patch("/{role_id}", response_model=RoleRead)
async def update_role(
    role_id: uuid.UUID,
    data: RoleUpdate,
    _user: User = Depends(require_permission(ROLES_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    if data.permissions is not None:
        invalid = set(data.permissions) - set(ALL_PERMISSIONS)
        if invalid:
            raise HTTPException(status_code=422, detail=f"Invalid permissions: {sorted(invalid)}")
        role.permissions = data.permissions

    if data.name is not None:
        # Check uniqueness
        exists = await db.execute(
            select(Role).where(Role.name == data.name, Role.id != role_id)
        )
        if exists.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Role name already exists")
        role.name = data.name

    if data.description is not None:
        role.description = data.description

    await db.commit()
    await db.refresh(role)
    return role


@router.delete("/{role_id}")
async def delete_role(
    role_id: uuid.UUID,
    _user: User = Depends(require_permission(ROLES_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    if role.is_builtin:
        raise HTTPException(status_code=400, detail="Cannot delete built-in roles")

    # Check if any users use this role
    user_count = await db.execute(
        select(func.count(User.id)).where(User.role_id == role_id)
    )
    if user_count.scalar() > 0:
        raise HTTPException(status_code=409, detail="Cannot delete role with assigned users")

    await db.delete(role)
    await db.commit()
    return {"deleted": str(role_id)}


@router.get("/permissions/catalogue")
async def permission_catalogue(
    _user: User = Depends(require_permission(ROLES_READ)),
):
    """Return the full list of valid permissions for UI dropdowns."""
    return {"permissions": ALL_PERMISSIONS}
