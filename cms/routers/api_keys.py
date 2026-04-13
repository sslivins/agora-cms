"""API key management routes."""

import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import _hash_api_key, require_auth, require_permission
from cms.database import get_db
from cms.models.api_key import APIKey
from cms.models.user import User
from cms.permissions import API_KEYS_READ, API_KEYS_WRITE

router = APIRouter(prefix="/api/keys", dependencies=[Depends(require_auth)])

KEY_PREFIX = "agora_"


class APIKeyCreate(BaseModel):
    name: str


class APIKeyOut(BaseModel):
    id: str
    name: str
    key_prefix: str
    created_at: datetime
    last_used_at: datetime | None


class APIKeyCreated(APIKeyOut):
    """Returned only on creation — includes the full key (shown once)."""
    key: str


def _generate_key() -> str:
    """Generate a random API key with agora_ prefix."""
    return KEY_PREFIX + secrets.token_hex(24)


@router.get("", response_model=list[APIKeyOut])
async def list_api_keys(
    user: User = Depends(require_permission(API_KEYS_READ)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(APIKey).order_by(APIKey.created_at.desc()))
    keys = result.scalars().all()
    return [
        APIKeyOut(
            id=str(k.id),
            name=k.name,
            key_prefix=k.key_prefix,
            created_at=k.created_at,
            last_used_at=k.last_used_at,
        )
        for k in keys
    ]


@router.post("", response_model=APIKeyCreated, status_code=201)
async def create_api_key(
    data: APIKeyCreate,
    user: User = Depends(require_permission(API_KEYS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    if not data.name.strip():
        raise HTTPException(status_code=422, detail="Name is required")

    raw_key = _generate_key()
    key_hash = _hash_api_key(raw_key)
    prefix = raw_key[:12] + "..."

    api_key = APIKey(
        name=data.name.strip(),
        key_prefix=prefix,
        key_hash=key_hash,
        user_id=user.id,
    )
    db.add(api_key)
    await db.commit()
    await db.refresh(api_key)

    return APIKeyCreated(
        id=str(api_key.id),
        name=api_key.name,
        key_prefix=api_key.key_prefix,
        created_at=api_key.created_at,
        last_used_at=api_key.last_used_at,
        key=raw_key,
    )


@router.delete("/{key_id}")
async def delete_api_key(
    key_id: uuid.UUID,
    user: User = Depends(require_permission(API_KEYS_WRITE)),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(APIKey).where(APIKey.id == key_id))
    api_key = result.scalar_one_or_none()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")
    await db.delete(api_key)
    await db.commit()
    return {"deleted": str(key_id)}
