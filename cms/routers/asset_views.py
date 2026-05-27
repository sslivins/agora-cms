"""Saved Views API for the asset library (Phase 3).

Each authenticated user can store a small set of named filter presets
("My recent uploads", "Untagged videos", etc.) and recall them from a
dropdown above the asset library.  Views are strictly per-user; there
is no admin escape hatch and no org-shared visibility in this phase.

Setting ``is_default=True`` on one view atomically clears the flag on
the user's other views.  The partial unique index on
``(user_id) WHERE is_default`` guarantees this at the DB level on
Postgres; SQLite ignores the partial WHERE clause but the application
layer still enforces the single-default invariant in the same txn.
"""

from __future__ import annotations

import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_current_user, get_settings, require_auth
from cms.config import Settings
from cms.database import get_db
from cms.models.asset_view import AssetView
from cms.models.user import User
from cms.schemas.asset_view import AssetViewIn, AssetViewOut, AssetViewPatch


router = APIRouter(prefix="/api/asset-views", dependencies=[Depends(require_auth)])


async def _current_user(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
) -> User:
    """Return the authenticated User.  Wraps ``get_current_user`` so the
    handlers can declare a simple ``user: User = Depends(_current_user)``
    parameter without each one repeating the request/settings/db plumbing.
    """
    return await get_current_user(request, settings, db)


async def _get_owned(view_id: uuid.UUID, user: User, db: AsyncSession) -> AssetView:
    """Fetch ``view_id`` or raise 404 if missing / not owned by ``user``."""
    view = (
        await db.execute(select(AssetView).where(AssetView.id == view_id))
    ).scalar_one_or_none()
    # 404 (not 403) on cross-user so we don't leak existence.
    if not view or view.user_id != user.id:
        raise HTTPException(status_code=404, detail="View not found")
    return view


async def _clear_other_defaults(user_id: uuid.UUID, except_id: uuid.UUID | None, db: AsyncSession) -> None:
    """Clear ``is_default`` on every view owned by ``user_id`` except ``except_id``.

    Run inside the caller's transaction; commit is the caller's
    responsibility.
    """
    stmt = (
        update(AssetView)
        .where(AssetView.user_id == user_id, AssetView.is_default.is_(True))
        .values(is_default=False)
    )
    if except_id is not None:
        stmt = stmt.where(AssetView.id != except_id)
    await db.execute(stmt)


async def _name_taken(user_id: uuid.UUID, name: str, except_id: uuid.UUID | None, db: AsyncSession) -> bool:
    stmt = select(AssetView.id).where(
        AssetView.user_id == user_id, AssetView.name == name
    )
    if except_id is not None:
        stmt = stmt.where(AssetView.id != except_id)
    return (await db.execute(stmt)).first() is not None


@router.get("", response_model=List[AssetViewOut])
async def list_views(
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    """List the caller's saved views, ordered by name."""
    rows = (
        await db.execute(
            select(AssetView)
            .where(AssetView.user_id == user.id)
            .order_by(AssetView.name)
        )
    ).scalars().all()
    return [AssetViewOut.model_validate(v) for v in rows]


@router.post("", response_model=AssetViewOut, status_code=201)
async def create_view(
    payload: AssetViewIn,
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Create a new saved view for the caller."""
    if await _name_taken(user.id, payload.name, None, db):
        raise HTTPException(
            status_code=409, detail=f"A view named {payload.name!r} already exists"
        )

    if payload.is_default:
        await _clear_other_defaults(user.id, None, db)
        await db.flush()

    view = AssetView(
        user_id=user.id,
        name=payload.name,
        filters=payload.filters.model_dump(exclude_none=True),
        is_default=payload.is_default,
    )
    db.add(view)
    await db.flush()

    await db.commit()
    await db.refresh(view)
    return AssetViewOut.model_validate(view)


@router.patch("/{view_id}", response_model=AssetViewOut)
async def update_view(
    view_id: uuid.UUID,
    payload: AssetViewPatch,
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Update name, filters, or default flag on a saved view."""
    view = await _get_owned(view_id, user, db)

    if payload.name is not None and payload.name != view.name:
        if await _name_taken(user.id, payload.name, view.id, db):
            raise HTTPException(
                status_code=409, detail=f"A view named {payload.name!r} already exists"
            )
        view.name = payload.name

    if payload.filters is not None:
        view.filters = payload.filters.model_dump(exclude_none=True)

    if payload.is_default is not None:
        if payload.is_default and not view.is_default:
            # Promote: clear other defaults first.
            await _clear_other_defaults(user.id, view.id, db)
        view.is_default = payload.is_default

    await db.flush()
    await db.commit()
    await db.refresh(view)
    return AssetViewOut.model_validate(view)


@router.delete("/{view_id}", status_code=204)
async def delete_view(
    view_id: uuid.UUID,
    user: User = Depends(_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Hard-delete a saved view.  No auto-promotion of another default."""
    view = await _get_owned(view_id, user, db)
    await db.execute(delete(AssetView).where(AssetView.id == view.id))
    await db.commit()
