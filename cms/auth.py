"""Authentication — session-based for web UI, API key for programmatic access."""

import hashlib
import logging
import uuid
from functools import lru_cache

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.config import Settings
from cms.database import get_db
from cms.models.api_key import APIKey
from cms.models.setting import CMSSetting
from cms.models.user import Role, User, UserGroup

_log = logging.getLogger(__name__)

COOKIE_NAME = "agora_cms_session"
MAX_AGE = 604800  # 7 days

SETTING_PASSWORD_HASH = "admin_password_hash"
SETTING_USERNAME = "admin_username"
SETTING_TIMEZONE = "timezone"
SETTING_MCP_ENABLED = "mcp_enabled"
SETTING_MCP_SERVICE_KEY_HASH = "mcp_service_key_hash"  # SHA-256 hash of the MCP service key
SETTING_MCP_ROLE_ID = "mcp_role_id"  # System-wide MCP permission ceiling
SETTING_API_ROLE_ID = "api_role_id"  # System-wide API key permission ceiling
SETTING_SMTP_HOST = "smtp_host"
SETTING_SMTP_PORT = "smtp_port"
SETTING_SMTP_USERNAME = "smtp_username"
SETTING_SMTP_PASSWORD = "smtp_password"
SETTING_SMTP_FROM_EMAIL = "smtp_from_email"


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
) -> tuple[User, APIKey] | None:
    """Look up the user associated with an API key.

    Returns ``(user, api_key_row)`` or ``None`` if the key is invalid.
    """
    key_hash = _hash_api_key(api_key_value)
    result = await db.execute(
        select(APIKey)
        .options(selectinload(APIKey.user).selectinload(User.role),
                selectinload(APIKey.user).selectinload(User.groups).selectinload(UserGroup.group))
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
        admin_user = admin.scalar_one_or_none()
        if admin_user is None:
            return None
        return admin_user, key_row
    return key_row.user, key_row


async def _resolve_user_from_session(
    cookie: str, settings: Settings, db: AsyncSession
) -> User | None:
    """Decode session cookie and return the User. Returns None if invalid."""
    serializer = URLSafeTimedSerializer(settings.secret_key)
    try:
        data = serializer.loads(cookie, max_age=MAX_AGE)
    except SignatureExpired as exc:
        # Tolerate small backward clock drift (common on Docker Desktop / VMs).
        # If the cookie was signed "in the future" by a few seconds due to
        # clock correction, the signature is still valid — retry without
        # the max_age check.
        if "< 0 seconds" in str(exc):
            try:
                data = serializer.loads(cookie)
            except BadSignature:
                return None
        else:
            return None
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
            .options(selectinload(User.role),
                     selectinload(User.groups).selectinload(UserGroup.group))
            .where(User.id == user_id, User.is_active.is_(True))
        )
        return result.scalar_one_or_none()

    # Legacy cookie (pre-RBAC) — look up the admin user by username
    if isinstance(data, str):
        result = await db.execute(
            select(User)
            .options(selectinload(User.role),
                     selectinload(User.groups).selectinload(UserGroup.group))
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
    Service keys (agora_svc_) get special handling — they authenticate as
    an admin user with the real user recorded via X-On-Behalf-Of header.
    MCP-type keys are blocked from REST API (MCP server uses the service key).
    """
    # API key auth
    api_key = request.headers.get("X-API-Key")
    if api_key:
        # Check if this is the MCP service key (prefix-gated for performance)
        if api_key.startswith(SERVICE_KEY_PREFIX):
            service_key_hash = await get_setting(db, SETTING_MCP_SERVICE_KEY_HASH)
            if service_key_hash and _hash_api_key(api_key) == service_key_hash:
                on_behalf_of = request.headers.get("X-On-Behalf-Of", "MCP Service")
                request.state.auth_method = "mcp_service"
                request.state.on_behalf_of = on_behalf_of
                # Return admin user for permission checks
                admin = await db.execute(
                    select(User)
                    .options(selectinload(User.role),
                             selectinload(User.groups).selectinload(UserGroup.group))
                    .join(User.role)
                    .where(Role.name == "Admin", User.is_active.is_(True))
                )
                admin_user = admin.scalar_one_or_none()
                if admin_user is None:
                    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                                        detail="No admin user available for service key")
                return admin_user
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                                detail="Invalid service key")

        # Regular user API key
        result = await _resolve_user_from_api_key(api_key, db)
        if result is None:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
        user, key_row = result

        # Block MCP keys from REST API — MCP server should use the service key
        if key_row.key_type == "mcp":
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="MCP keys cannot access the REST API directly. "
                       "Use an API key or the MCP server.",
            )

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
    Also redirects users who must change their password.
    """
    user = await get_current_user(request, settings, db)
    request.state.user = user

    # Force password change redirect for web UI requests
    if user.must_change_password and request.url.path != "/force-password-change":
        from fastapi.responses import RedirectResponse
        raise HTTPException(
            status_code=status.HTTP_307_TEMPORARY_REDIRECT,
            headers={"Location": "/force-password-change"},
        )


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
        request.state.user = user
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

    Users with the 'groups:view_all' permission return None, meaning "no filtering"
    — they are treated as if they are a member of every group.
    Non-admin users return their assigned group IDs for query scoping.
    """
    from cms.permissions import GROUPS_VIEW_ALL
    if GROUPS_VIEW_ALL in user.role.permissions:
        return None  # Admin — no group scoping
    result = await db.execute(
        select(UserGroup.group_id).where(UserGroup.user_id == user.id)
    )
    return [row[0] for row in result.all()]


async def verify_resource_group_access(
    user: User, db: AsyncSession, group_id: uuid.UUID | None
) -> None:
    """Raise 403 if the user does not have access to the given group.

    Call this on every by-ID endpoint after fetching the resource to prevent
    IDOR attacks. ``group_id`` may be ``None`` (e.g. a device not yet assigned
    to a group), in which case access is allowed (the resource is unscoped).
    """
    if group_id is None:
        return  # Resource has no group — allow access
    group_ids = await get_user_group_ids(user, db)
    if group_ids is None:
        return  # User has groups:view_all — allow all
    if group_id not in group_ids:
        raise HTTPException(status_code=403, detail="Access denied: resource is outside your assigned groups")


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


async def delete_setting(db: AsyncSession, key: str) -> None:
    result = await db.execute(select(CMSSetting).where(CMSSetting.key == key))
    setting = result.scalar_one_or_none()
    if setting:
        await db.delete(setting)
        await db.commit()


# ── MCP service key management ──

SERVICE_KEY_PREFIX = "agora_svc_"


def generate_service_key() -> str:
    """Generate a random MCP service key with agora_svc_ prefix."""
    import secrets
    return SERVICE_KEY_PREFIX + secrets.token_hex(32)


def write_service_key_file(raw_key: str, path: str) -> None:
    """Write the raw service key to the shared volume file."""
    import os
    from pathlib import Path

    key_path = Path(path)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(raw_key)
    # Restrict permissions (owner read/write only) — best-effort on Windows
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def clear_service_key_file(path: str) -> None:
    """Remove the service key file from the shared volume."""
    from pathlib import Path

    key_path = Path(path)
    if key_path.exists():
        key_path.unlink()


async def provision_service_key(db: AsyncSession, path: str) -> str:
    """Generate a new service key, store its hash, and write to the shared volume.

    Returns the key prefix for display in the UI.
    """
    raw_key = generate_service_key()
    key_hash = _hash_api_key(raw_key)
    await set_setting(db, SETTING_MCP_SERVICE_KEY_HASH, key_hash)
    write_service_key_file(raw_key, path)
    return raw_key[:16] + "..."


async def revoke_service_key(db: AsyncSession, path: str) -> None:
    """Delete the service key hash from settings and clear the file."""
    await delete_setting(db, SETTING_MCP_SERVICE_KEY_HASH)
    clear_service_key_file(path)


async def compute_effective_permissions(
    user: User, key_type: str, db: AsyncSession
) -> list[str]:
    """Compute effective permissions by intersecting user role with key-type role ceiling.

    If no key-type role is configured, the user's own permissions are returned unchanged.
    """
    user_perms = set(user.role.permissions) if user.role else set()

    setting_key = SETTING_MCP_ROLE_ID if key_type == "mcp" else SETTING_API_ROLE_ID
    role_id_str = await get_setting(db, setting_key)
    if not role_id_str:
        return list(user_perms)

    import uuid as _uuid
    try:
        role_id = _uuid.UUID(role_id_str)
    except ValueError:
        return list(user_perms)

    result = await db.execute(select(Role).where(Role.id == role_id))
    ceiling_role = result.scalar_one_or_none()
    if ceiling_role is None:
        return list(user_perms)

    ceiling_perms = set(ceiling_role.permissions)
    return list(user_perms & ceiling_perms)


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

    # Check if admin User row exists (try by username first for backward compat)
    result = await db.execute(
        select(User).where(User.username == settings.admin_username)
    )
    admin_user = result.scalar_one_or_none()

    admin_email = settings.admin_email

    if admin_user is None:
        # Create admin user from env vars
        admin_user = User(
            username=settings.admin_username,
            email=admin_email,
            display_name="Administrator",
            password_hash=hash_password(settings.admin_password),
            role_id=admin_role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(admin_user)
        await db.commit()
        _log.info("Created admin user: %s", settings.admin_username)
    else:
        # Ensure email is set (migration for existing admin users)
        if not admin_user.email:
            admin_user.email = admin_email
        if settings.reset_password:
            admin_user.password_hash = hash_password(settings.admin_password)
            await db.commit()
            _log.warning(
                "Admin password reset from environment. "
                "Set AGORA_CMS_RESET_PASSWORD=false and restart."
            )

    # Keep cms_settings in sync for any legacy code
    await set_setting(db, SETTING_PASSWORD_HASH, admin_user.password_hash)
    await set_setting(db, SETTING_USERNAME, admin_user.username)
