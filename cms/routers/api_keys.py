"""API key management routes.

Self-service endpoints (``/api/keys/my``) let users manage their own keys.
Admin endpoints (``/api/keys``) provide oversight of all users' keys.
"""

import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import _hash_api_key, require_auth, require_permission
from cms.database import get_db
from cms.models.api_key import APIKey
from cms.models.notification import Notification
from cms.models.user import User
from cms.permissions import (
    API_KEYS_MANAGE,
    API_KEYS_SELF,
    MCP_KEYS_SELF,
)
from cms.services.audit_service import audit_log

router = APIRouter(prefix="/api/keys", dependencies=[Depends(require_auth)])

KEY_PREFIX = "agora_"

VALID_KEY_TYPES = {"mcp", "api"}


# ── Pydantic schemas ──────────────────────────────────────────────

class APIKeyCreate(BaseModel):
    name: str
    key_type: str = "api"


class APIKeyOut(BaseModel):
    id: str
    name: str
    key_prefix: str
    key_type: str
    created_at: datetime
    last_used_at: datetime | None
    user_id: str | None = None
    user_display_name: str | None = None


class APIKeyCreated(APIKeyOut):
    """Returned only on creation/regeneration — includes the full key (shown once)."""
    key: str


# ── Helpers ───────────────────────────────────────────────────────

def _generate_key() -> str:
    """Generate a random API key with agora_ prefix."""
    return KEY_PREFIX + secrets.token_hex(24)


def _key_to_out(k: APIKey, include_user: bool = False) -> APIKeyOut:
    """Convert an APIKey model instance to an APIKeyOut response."""
    return APIKeyOut(
        id=str(k.id),
        name=k.name,
        key_prefix=k.key_prefix,
        key_type=k.key_type,
        created_at=k.created_at,
        last_used_at=k.last_used_at,
        user_id=str(k.user_id) if include_user and k.user_id else None,
        user_display_name=(
            (k.user.display_name or k.user.username) if include_user and k.user else None
        ),
    )


def _require_self_permission(user: User, key_type: str) -> None:
    """Raise 403 if the user lacks the self-service permission for this key type."""
    perms = user.role.permissions if user.role else []
    if key_type == "mcp" and MCP_KEYS_SELF not in perms:
        raise HTTPException(status_code=403, detail="You don't have permission to manage MCP keys")
    if key_type == "api" and API_KEYS_SELF not in perms:
        raise HTTPException(status_code=403, detail="You don't have permission to manage API keys")


# ── Self-service endpoints: /api/keys/my ─────────────────────────

@router.get("/my", response_model=list[APIKeyOut])
async def list_my_keys(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """List the current user's own keys."""
    user: User = request.state.user
    result = await db.execute(
        select(APIKey)
        .where(APIKey.user_id == user.id)
        .order_by(APIKey.created_at.desc())
    )
    return [_key_to_out(k) for k in result.scalars().all()]


@router.post("/my", response_model=APIKeyCreated, status_code=201)
async def create_my_key(
    data: APIKeyCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Create a new key for the current user."""
    user: User = request.state.user

    if data.key_type not in VALID_KEY_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid key type: {data.key_type}")
    if not data.name.strip():
        raise HTTPException(status_code=422, detail="Name is required")

    _require_self_permission(user, data.key_type)

    raw_key = _generate_key()
    key_hash = _hash_api_key(raw_key)
    prefix = raw_key[:12] + "..."

    api_key = APIKey(
        name=data.name.strip(),
        key_prefix=prefix,
        key_hash=key_hash,
        key_type=data.key_type,
        user_id=user.id,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreated(
        id=str(api_key.id),
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        key_type=api_key.key_type,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        key=raw_key,
    )


@router.post("/my/{key_id}/regenerate", response_model=APIKeyCreated)
async def regenerate_my_key(
    key_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Regenerate an own key — same name/type, new secret."""
    user: User = request.state.user
    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    api_key = result.scalar_one_or_none()
    if not api_key or api_key.user_id != user.id:
        raise HTTPException(status_code=404, detail="Key not found")

    _require_self_permission(user, api_key.key_type)

    raw_key = _generate_key()
    api_key.key_hash = _hash_api_key(raw_key)
    api_key.key_prefix = raw_key[:12] + "..."
    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreated(
        id=str(api_key.id),
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        key_type=api_key.key_type,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        key=raw_key,
    )


@router.delete("/my/{key_id}")
async def delete_my_key(
    key_id: uuid.UUID,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Revoke (delete) an own key."""
    user: User = request.state.user
    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    api_key = result.scalar_one_or_none()
    if not api_key or api_key.user_id != user.id:
        raise HTTPException(status_code=404, detail="Key not found")

    _require_self_permission(user, api_key.key_type)

    await db.delete(api_key)
    await db.commit()
    return {"deleted": str(key_id)}


# ── Admin endpoints: /api/keys (requires api_keys:manage) ───────

@router.get("", response_model=list[APIKeyOut])
async def list_all_keys(
    user: User = Depends(require_permission(API_KEYS_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    """List ALL keys across all users (admin oversight)."""
    result = await db.execute(
        select(APIKey)
        .options(selectinload(APIKey.user))
        .order_by(APIKey.created_at.desc())
    )
    return [_key_to_out(k, include_user=True) for k in result.scalars().all()]


@router.post("", response_model=APIKeyCreated, status_code=201)
async def create_api_key(
    data: APIKeyCreate,
    user: User = Depends(require_permission(API_KEYS_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    """Admin: create a key (attributed to the admin user)."""
    if data.key_type not in VALID_KEY_TYPES:
        raise HTTPException(status_code=422, detail=f"Invalid key type: {data.key_type}")
    if not data.name.strip():
        raise HTTPException(status_code=422, detail="Name is required")

    raw_key = _generate_key()
    key_hash = _hash_api_key(raw_key)
    prefix = raw_key[:12] + "..."

    api_key = APIKey(
        name=data.name.strip(),
        key_prefix=prefix,
        key_hash=key_hash,
        key_type=data.key_type,
        user_id=user.id,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreated(
        id=str(api_key.id),
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        key_type=api_key.key_type,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        key=raw_key,
    )


@router.post("/{key_id}/regenerate", response_model=APIKeyCreated)
async def regenerate_api_key(
    key_id: uuid.UUID,
    user: User = Depends(require_permission(API_KEYS_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    """Admin: regenerate any key — same name/type, new secret."""
    result = await db.execute(
        select(APIKey).where(APIKey.id == key_id).options(selectinload(APIKey.user))
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    raw_key = _generate_key()
    api_key.key_hash = _hash_api_key(raw_key)
    api_key.key_prefix = raw_key[:12] + "..."

    await audit_log(db, user=user, action="api_key.regenerate",
                    resource_type="api_key", resource_id=str(key_id),
                    details={"key_name": api_key.name, "owner_id": str(api_key.user_id)})

    # Notify key owner if it's not the admin's own key
    if api_key.user_id and api_key.user_id != user.id:
        admin_name = user.display_name or user.username
        db.add(Notification(
            scope="user",
            level="warning",
            title="API key regenerated",
            message=f'Your key "{api_key.name}" was regenerated by {admin_name}. The old secret is no longer valid.',
            user_id=api_key.user_id,
            details={"key_name": api_key.name, "admin": admin_name},
        ))

    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreated(
        id=str(api_key.id),
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        key_type=api_key.key_type,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        key=raw_key,
    )


@router.delete("/{key_id}")
async def delete_api_key(
    key_id: uuid.UUID,
    user: User = Depends(require_permission(API_KEYS_MANAGE)),
    db: AsyncSession = Depends(get_db),
):
    """Admin: revoke (delete) any key."""
    result = await db.execute(
        select(APIKey).where(APIKey.id == key_id).options(selectinload(APIKey.user))
    )
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    key_name = api_key.name
    owner_id = api_key.user_id

    await audit_log(db, user=user, action="api_key.revoke",
                    resource_type="api_key", resource_id=str(key_id),
                    details={"key_name": key_name, "owner_id": str(owner_id)})
    await db.delete(api_key)

    # Notify key owner if it's not the admin's own key
    if owner_id and owner_id != user.id:
        admin_name = user.display_name or user.username
        db.add(Notification(
            scope="user",
            level="warning",
            title="API key revoked",
            message=f'Your key "{key_name}" was revoked by {admin_name}. Any services using this key will lose access.',
            user_id=owner_id,
            details={"key_name": key_name, "admin": admin_name},
        ))

    await db.commit()
    return {"deleted": str(key_id)}
