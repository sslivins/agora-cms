"""User management API routes."""

import secrets
import uuid

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import get_current_user, hash_password, require_permission, verify_password
from cms.database import get_db
from cms.models.user import Role, User, UserGroup
from cms.permissions import USERS_READ, USERS_WRITE
from cms.schemas.user import PasswordChange, UserCreate, UserMe, UserRead, UserUpdate
from cms.services.audit_service import audit_log

router = APIRouter(prefix="/api/users")


def _user_to_read(user: User, group_ids: list[uuid.UUID] | None = None) -> UserRead:
    """Convert a User ORM object to a UserRead schema."""
    return UserRead(
        id=user.id,
        email=user.email,
        display_name=user.display_name,
        role_id=user.role_id,
        role=user.role if user.role else None,
        is_active=user.is_active,
        must_change_password=user.must_change_password,
        created_at=user.created_at,
        updated_at=user.updated_at,
        last_login_at=user.last_login_at,
        group_ids=group_ids or [],
    )


async def _get_group_ids(user_id: uuid.UUID, db: AsyncSession) -> list[uuid.UUID]:
    result = await db.execute(
        select(UserGroup.group_id).where(UserGroup.user_id == user_id)
    )
    return [row[0] for row in result.all()]


@router.get("/me", response_model=UserMe)
async def get_my_profile(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Return the current user's profile and permissions."""
    group_ids = await _get_group_ids(current_user.id, db)
    return UserMe(
        id=current_user.id,
        email=current_user.email,
        display_name=current_user.display_name,
        role=current_user.role,
        group_ids=group_ids,
        permissions=current_user.role.permissions if current_user.role else [],
    )


@router.post("/me/password")
async def change_my_password(
    data: PasswordChange,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Change the current user's own password."""
    if not verify_password(data.current_password, current_user.password_hash):
        raise HTTPException(status_code=400, detail="Current password is incorrect")
    current_user.password_hash = hash_password(data.new_password)
    current_user.must_change_password = False
    await db.commit()
    return {"status": "ok"}


@router.get("", response_model=list[UserRead])
async def list_users(
    _user: User = Depends(require_permission(USERS_READ)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User)
        .options(selectinload(User.role))
        .order_by(User.email)
    )
    users = result.scalars().all()
    out = []
    for u in users:
        gids = await _get_group_ids(u.id, db)
        out.append(_user_to_read(u, gids))
    return out


@router.get("/{user_id}", response_model=UserRead)
async def get_user(
    user_id: uuid.UUID,
    _user: User = Depends(require_permission(USERS_READ)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User)
        .options(selectinload(User.role))
        .where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    gids = await _get_group_ids(user.id, db)
    return _user_to_read(user, gids)


@router.post("", response_model=UserRead, status_code=201)
async def create_user(
    data: UserCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    _user: User = Depends(require_permission(USERS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    # Require SMTP to be configured before creating users
    from cms.services.email_service import get_smtp_settings
    smtp_cfg = await get_smtp_settings(db)
    if not smtp_cfg.get("host") or not smtp_cfg.get("from_email"):
        raise HTTPException(
            status_code=422,
            detail="SMTP is not configured. Set up email in Settings → SMTP before creating user accounts.",
        )

    # Check email uniqueness
    exists = await db.execute(select(User).where(User.email == data.email))
    if exists.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Email already exists")

    # Verify role exists
    role = await db.execute(select(Role).where(Role.id == data.role_id))
    if not role.scalar_one_or_none():
        raise HTTPException(status_code=422, detail="Role not found")

    # Generate temporary password if none provided
    temp_password = data.password or secrets.token_urlsafe(12)

    # Use email prefix as username (internal field)
    username = data.email.split("@")[0]
    # Ensure uniqueness by appending suffix if needed
    base_username = username
    counter = 1
    while True:
        check = await db.execute(select(User).where(User.username == username))
        if not check.scalar_one_or_none():
            break
        username = f"{base_username}{counter}"
        counter += 1

    # Generate one-time setup token for magic login link
    setup_token = secrets.token_urlsafe(32)

    user = User(
        username=username,
        email=data.email,
        display_name=data.display_name,
        password_hash=hash_password(temp_password),
        role_id=data.role_id,
        is_active=True,
        must_change_password=True,
        setup_token=setup_token,
    )
    db.add(user)
    await db.flush()

    # Assign groups
    for gid in data.group_ids:
        db.add(UserGroup(user_id=user.id, group_id=gid))

    await audit_log(db, user=_user, action="user.create", resource_type="user",
                    resource_id=str(user.id), details={"email": data.email},
                    request=request)
    await db.commit()
    await db.refresh(user, ["role"])

    # Send welcome email in background — failures create a notification
    from cms.services.email_service import send_welcome_email_background
    base = request.base_url._url.rstrip("/")
    setup_url = f"{base}/setup-account?token={setup_token}"
    background_tasks.add_task(
        send_welcome_email_background,
        smtp_cfg=smtp_cfg,
        to_email=data.email,
        display_name=data.display_name,
        temp_password=temp_password,
        setup_url=setup_url,
    )

    return _user_to_read(user, data.group_ids)


@router.patch("/{user_id}", response_model=UserRead)
async def update_user(
    user_id: uuid.UUID,
    data: UserUpdate,
    request: Request,
    _admin: User = Depends(require_permission(USERS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(User).options(selectinload(User.role)).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if data.email is not None:
        exists = await db.execute(
            select(User).where(User.email == data.email, User.id != user_id)
        )
        if exists.scalar_one_or_none():
            raise HTTPException(status_code=409, detail="Email already exists")
        user.email = data.email
    if data.display_name is not None:
        user.display_name = data.display_name
    if data.password is not None:
        user.password_hash = hash_password(data.password)
    if data.role_id is not None:
        role = await db.execute(select(Role).where(Role.id == data.role_id))
        if not role.scalar_one_or_none():
            raise HTTPException(status_code=422, detail="Role not found")
        user.role_id = data.role_id
    if data.is_active is not None:
        user.is_active = data.is_active
    if data.must_change_password is not None:
        user.must_change_password = data.must_change_password

    if data.group_ids is not None:
        # Replace group assignments
        from sqlalchemy import delete
        await db.execute(
            delete(UserGroup).where(UserGroup.user_id == user_id)
        )
        for gid in data.group_ids:
            db.add(UserGroup(user_id=user.id, group_id=gid))

    details = data.model_dump(exclude_unset=True, exclude={"password"}, mode="json")
    await audit_log(db, user=_admin, action="user.update", resource_type="user",
                    resource_id=str(user_id),
                    details=details,
                    request=request)
    await db.commit()
    await db.refresh(user, ["role"])
    gids = await _get_group_ids(user.id, db)
    return _user_to_read(user, gids)


@router.delete("/{user_id}")
async def delete_user(
    user_id: uuid.UUID,
    request: Request,
    _admin: User = Depends(require_permission(USERS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    # Prevent deleting yourself
    if user.id == _admin.id:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")

    # Remove group assignments
    from sqlalchemy import delete
    await db.execute(delete(UserGroup).where(UserGroup.user_id == user_id))

    await audit_log(db, user=_admin, action="user.delete", resource_type="user",
                    resource_id=str(user_id), details={"email": user.email},
                    request=request)
    await db.delete(user)
    await db.commit()
    return {"deleted": str(user_id)}


@router.post("/{user_id}/resend-invite")
async def resend_invite(
    user_id: uuid.UUID,
    request: Request,
    background_tasks: BackgroundTasks,
    _admin: User = Depends(require_permission(USERS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    """Resend the activation email for a user who hasn't completed setup."""
    result = await db.execute(
        select(User).options(selectinload(User.role)).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if not user.must_change_password:
        raise HTTPException(
            status_code=400,
            detail="User has already completed account setup",
        )

    # Require SMTP
    from cms.services.email_service import get_smtp_settings, send_welcome_email_background
    smtp_cfg = await get_smtp_settings(db)
    if not smtp_cfg.get("host") or not smtp_cfg.get("from_email"):
        raise HTTPException(
            status_code=422,
            detail="SMTP is not configured. Set up email in Settings → SMTP first.",
        )

    # Regenerate setup token (invalidates old link)
    new_token = secrets.token_urlsafe(32)
    user.setup_token = new_token
    await db.commit()

    base = request.base_url._url.rstrip("/")
    setup_url = f"{base}/setup-account?token={new_token}"
    background_tasks.add_task(
        send_welcome_email_background,
        smtp_cfg=smtp_cfg,
        to_email=user.email,
        display_name=user.display_name,
        temp_password="(use the link below to set your password)",
        setup_url=setup_url,
    )

    return {"status": "ok", "message": f"Activation email queued for {user.email}"}
