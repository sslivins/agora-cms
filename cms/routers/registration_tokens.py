"""Registration token management API routes."""

import secrets
from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import require_auth
from cms.database import get_db
from cms.models.registration_token import RegistrationToken
from cms.schemas.registration_token import RegistrationTokenCreate, RegistrationTokenOut

router = APIRouter(
    prefix="/api/tokens",
    dependencies=[Depends(require_auth)],
)


@router.get("", response_model=List[RegistrationTokenOut])
async def list_tokens(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(RegistrationToken).order_by(RegistrationToken.created_at.desc())
    )
    return result.scalars().all()


@router.post("", response_model=RegistrationTokenOut, status_code=201)
async def create_token(
    data: RegistrationTokenCreate,
    db: AsyncSession = Depends(get_db),
):
    token = RegistrationToken(
        token=secrets.token_urlsafe(32),
        label=data.label,
        max_uses=data.max_uses,
        expires_at=data.expires_at,
    )
    db.add(token)
    await db.commit()
    await db.refresh(token)
    return token


@router.delete("/{token_id}")
async def delete_token(token_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(RegistrationToken).where(RegistrationToken.id == token_id)
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    await db.delete(token)
    await db.commit()
    return {"deleted": str(token.id)}


@router.post("/{token_id}/revoke")
async def revoke_token(token_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(RegistrationToken).where(RegistrationToken.id == token_id)
    )
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail="Token not found")
    token.is_active = False
    await db.commit()
    return {"revoked": str(token.id)}
