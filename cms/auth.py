"""Authentication — session-based for web UI, API key for programmatic access."""

import hashlib
import logging
from functools import lru_cache

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.config import Settings
from cms.database import get_db
from cms.models.api_key import APIKey
from cms.models.setting import CMSSetting
from cms.models.user import Role, User

_log = logging.getLogger(__name__)

COOKIE_NAME = "agora_cms_session"
MAX_AGE = 604800  # 7 days

SETTING_PASSWORD_HASH = "admin_password_hash"
SETTING_USERNAME = "admin_username"
SETTING_TIMEZONE = "timezone"
SETTING_MCP_ENABLED = "mcp_enabled"
SETTING_MCP_API_KEY = "mcp_api_key"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def get_serializer(settings: Settings = Depends(get_settings)) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key)


def _hash_api_key(key: str) -> str:
    """Hash an API key with SHA-256 for storage/comparison."""
    return hashlib.sha256(key.encode()).hexdigest()


async def _resolve_user_from_api_key(
    api_key_value: str, db: AsyncSession
) -> User | None:
    """Look up the user associated with an API key. Returns None if invalid."""
    key_hash = _hash_api_key(api_key_value)
    result = await db.execute(
        select(APIKey)
        .options(selectinload(APIKey.user).selectinload(User.role))
        .where(APIKey.key_hash == key_hash)
    )
    key_row = result.scalar_one_or_none()
    if key_row is None:
        return None
    from datetime import datetime, timezone
    key_row.last_used_at = datetime.now(timezone.utc)
    await db.commit()
    # Legacy keys without a user_id: fall back to admin user
    if key_row.user is None:
        admin = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .join(User.role)
            .where(Role.name == "Admin", User.is_active.is_(True))
        )
        return admin.scalar_one_or_none()
    return key_row.user


async def _resolve_user_from_session(
    cookie: str, settings: Settings, db: AsyncSession
) -> User | None:
    """Decode session cookie and return the User. Returns None if invalid."""
    serializer = URLSafeTimedSerializer(settings.secret_key)
    try:
        data = serializer.loads(cookie, max_age=MAX_AGE)
    except BadSignature:
        return None

    # New-style cookie: {"user_id": "..."} — old-style: just "admin"
    if isinstance(data, dict) and "user_id" in data:
        import uuid as _uuid
        try:
            user_id = _uuid.UUID(data["user_id"])
        except (ValueError, AttributeError):
            return None
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(User.id == user_id, User.is_active.is_(True))
        )
        return result.scalar_one_or_none()

    # Legacy cookie (pre-RBAC) — look up the admin user by username
    if isinstance(data, str):
        result = await db.execute(
            select(User)
            .options(selectinload(User.role))
            .where(User.username == data, User.is_active.is_(True))
        )
        return result.scalar_one_or_none()

    return None


async def get_current_user(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Return the authenticated User or raise 401.

    Checks API key header first, then session cookie.
    """
    # API key auth
    api_key = request.headers.get("X-API-Key")
    if api_key:
        user = await _resolve_user_from_api_key(api_key, db)
        if user is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        return user

    # Session cookie auth
    cookie = request.cookies.get(COOKIE_NAME)
    if cookie:
        user = await _resolve_user_from_session(cookie, settings, db)
        if user is not None:
            return user

    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


async def require_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Legacy auth check — kept for backward compatibility during migration.

    Delegates to get_current_user but stores the user on request.state
    so templates can access user permissions for nav rendering.
    """
    user = await get_current_user(request, settings, db)
    request.state.user = user


def require_permission(*perms: str):
    """Return a FastAPI dependency that enforces one or more permissions.

    Usage::

        @router.get("/things", dependencies=[Depends(require_permission("things:read"))])
        async def list_things(...): ...

    Or inject the user directly::

        @router.post("/things")
        async def create_thing(user: User = Depends(require_permission("things:write"))): ...
    """
    from cms.permissions import has_permission

    async def _check(
        request: Request,
        settings: Settings = Depends(get_settings),
        db: AsyncSession = Depends(get_db),
    ) -> User:
        user = await get_current_user(request, settings, db)
        if user.role is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="No role assigned")
        for perm in perms:
            if not has_permission(user.role.permissions, perm):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=f"Missing permission: {perm}",
                )
        return user

    return _check


async def get_user_group_ids(user: User, db: AsyncSession) -> list | None:
    """Return the list of group UUIDs a user is assigned to, or None for admins.

    Admins (whose role has ALL permissions) return None, meaning "no filtering".
    Non-admin users return their assigned group IDs for query scoping.
    """
    from cms.permissions import ALL_PERMISSIONS
    if set(ALL_PERMISSIONS).issubset(set(user.role.permissions)):
        return None  # Admin — no group scoping
    from cms.models.user import UserGroup
    result = await db.execute(
        select(UserGroup.group_id).where(UserGroup.user_id == user.id)
    )
    return [row[0] for row in result.all()]


async def get_setting(db: AsyncSession, key: str) -> str | None:
    result = await db.execute(select(CMSSetting).where(CMSSetting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    result = await db.execute(select(CMSSetting).where(CMSSetting.key == key))
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = value
    else:
        db.add(CMSSetting(key=key, value=value))
    await db.commit()


async def ensure_admin_credentials(db: AsyncSession, settings: Settings) -> None:
    """On startup, ensure a User row exists for the admin.

    Migration path:
    1. If a User with the admin username already exists, optionally reset password.
    2. If no User exists yet, create one with the Admin role and env-var credentials.
    3. Keep cms_settings in sync for any legacy code that still reads them.
    """
    # Ensure built-in Admin role exists (should be seeded by _seed_roles first)
    result = await db.execute(select(Role).where(Role.name == "Admin"))
    admin_role = result.scalar_one_or_none()
    if admin_role is None:
        _log.error("Admin role not found — cannot seed admin user. Ensure _seed_roles runs first.")
        return

    # Check if admin User row exists
    result = await db.execute(
        select(User).where(User.username == settings.admin_username)
    )
    admin_user = result.scalar_one_or_none()

    if admin_user is None:
        # Create admin user from env vars
        admin_user = User(
            username=settings.admin_username,
            display_name="Administrator",
            password_hash=hash_password(settings.admin_password),
            role_id=admin_role.id,
            is_active=True,
        )
        db.add(admin_user)
        await db.commit()
        _log.info("Created admin user: %s", settings.admin_username)
    elif settings.reset_password:
        admin_user.password_hash = hash_password(settings.admin_password)
        await db.commit()
        _log.warning(
            "Admin password reset from environment. "
            "Set AGORA_CMS_RESET_PASSWORD=false and restart."
        )

    # Keep cms_settings in sync for any legacy code
    await set_setting(db, SETTING_PASSWORD_HASH, admin_user.password_hash)
    await set_setting(db, SETTING_USERNAME, admin_user.username)
