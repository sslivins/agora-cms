"""Admin API for feature flags.

Read is gated on ``features:read`` and write on ``features:write`` — separate
from ``settings:*`` on purpose.  Deciding who gets to see a half-built feature
is a different call from changing SMTP or token budgets, and the separation is
what lets the flag administration surface stay reachable no matter what any
flag is set to.

The listing is driven by the code registry rather than the ``feature_flags``
table, so a newly declared flag appears the moment it ships — nobody has to
seed a row first, and a flag with no row reports its declared default.

Both catalogs (users and roles) are returned with the listing so the UI can
render name-based pickers without a second round-trip; targeting is stored by
id but must never be *administered* by id.
"""

from __future__ import annotations

import logging
import uuid
from datetime import date, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import require_permission
from cms.database import get_db
from cms.models.user import Role, User
from cms.permissions import FEATURES_READ, FEATURES_WRITE
from cms.services import feature_flags as ff
from cms.services.audit_service import audit_log

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/features", tags=["features"])


# ── Schemas ──────────────────────────────────────────────────────────


class UserCatalogEntry(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str | None = None
    email: str | None = None


class RoleCatalogEntry(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None = None


class FlagOut(BaseModel):
    name: str
    description: str
    owner: str
    kind: str
    state: str
    user_ids: list[uuid.UUID] = Field(default_factory=list)
    role_ids: list[uuid.UUID] = Field(default_factory=list)
    expires: date | None = None
    # True when no row exists yet, i.e. the declared default is in effect.
    is_default: bool = False
    # A release toggle past its expiry: shipped, but never cleaned up.
    is_overdue: bool = False
    updated_at: datetime | None = None
    updated_by_id: uuid.UUID | None = None
    # Whether settings:write holders bypass targeting for this flag.  Surfaced
    # so the UI can say so rather than leaving admins guessing why they can
    # still see something that is targeted away from them.
    include_admins: bool = False


class FeaturesOut(BaseModel):
    flags: list[FlagOut]
    users: list[UserCatalogEntry]
    roles: list[RoleCatalogEntry]


class FlagStateIn(BaseModel):
    state: str
    user_ids: list[uuid.UUID] = Field(default_factory=list)
    role_ids: list[uuid.UUID] = Field(default_factory=list)


# ── Helpers ──────────────────────────────────────────────────────────


def _to_out(view: ff.FlagView) -> FlagOut:
    flag = ff.REGISTRY.get(view.name)
    return FlagOut(
        name=view.name,
        description=view.description,
        owner=view.owner,
        kind=view.kind.value,
        state=view.state.value,
        user_ids=[uuid.UUID(u) for u in view.user_ids],
        role_ids=[uuid.UUID(r) for r in view.role_ids],
        expires=view.expires,
        is_default=view.is_default,
        is_overdue=view.is_overdue,
        updated_at=view.updated_at,
        updated_by_id=view.updated_by_id,
        include_admins=flag.include_admins if flag is not None else False,
    )


async def _catalogs(
    db: AsyncSession,
) -> tuple[list[UserCatalogEntry], list[RoleCatalogEntry]]:
    users = (
        await db.execute(
            select(User).where(User.is_active == True).order_by(User.username)  # noqa: E712
        )
    ).scalars().all()
    roles = (await db.execute(select(Role).order_by(Role.name))).scalars().all()
    return (
        [
            UserCatalogEntry(
                id=u.id,
                username=u.username,
                display_name=u.display_name,
                email=u.email,
            )
            for u in users
        ],
        [
            RoleCatalogEntry(id=r.id, name=r.name, description=r.description)
            for r in roles
        ],
    )


async def _payload(db: AsyncSession) -> FeaturesOut:
    views = await ff.get_all(db)
    users, roles = await _catalogs(db)
    return FeaturesOut(
        flags=[_to_out(v) for v in views], users=users, roles=roles
    )


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("", response_model=FeaturesOut)
async def list_features(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(FEATURES_READ)),
) -> FeaturesOut:
    return await _payload(db)


@router.put("/{name}", response_model=FeaturesOut)
async def set_feature_state(
    name: str,
    payload: FlagStateIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission(FEATURES_WRITE)),
) -> FeaturesOut:
    try:
        state = ff.FlagState(payload.state)
    except ValueError:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Unknown state.",
                "allowed": [s.value for s in ff.FlagState],
            },
        )

    if name not in ff.REGISTRY:
        raise HTTPException(status_code=404, detail=f"Unknown feature flag {name!r}.")

    # Reject unknown ids rather than silently dropping them: a dropped id looks
    # to the admin like the grant succeeded, and the person it was meant for
    # never gets the feature.
    if state is ff.FlagState.TARGETED:
        await _reject_unknown(db, User, payload.user_ids, "user_ids")
        await _reject_unknown(db, Role, payload.role_ids, "role_ids")

    view = await ff.set_state(
        db,
        name,
        state=state,
        user_ids=payload.user_ids,
        role_ids=payload.role_ids,
        actor=_user,
    )
    await audit_log(
        db,
        user=_user,
        action="features.state.update",
        resource_type="feature_flag",
        resource_id=name,
        description=(
            f"Set feature '{name}' to {state.value}"
            + (
                f" for {len(payload.user_ids)} users and "
                f"{len(payload.role_ids)} roles"
                if state is ff.FlagState.TARGETED
                else ""
            )
        ),
        details={
            "state": state.value,
            "user_ids": [str(u) for u in view.user_ids],
            "role_ids": [str(r) for r in view.role_ids],
        },
        request=request,
    )
    await db.commit()
    return await _payload(db)


async def _reject_unknown(
    db: AsyncSession, model, ids: list[uuid.UUID], field: str
) -> None:
    if not ids:
        return
    known = {
        row.id
        for row in (
            await db.execute(select(model).where(model.id.in_(ids)))
        ).scalars().all()
    }
    unknown = [str(i) for i in ids if i not in known]
    if unknown:
        raise HTTPException(
            status_code=400,
            detail={"message": f"Unknown {field}.", f"unknown_{field}": unknown},
        )
