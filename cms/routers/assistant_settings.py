"""Admin API for the in-CMS Assistant settings (PR 6b of 6).

Three endpoints, all gated on ``settings:write``:

* ``GET  /api/settings/assistant`` — full state for the admin UI:
  current allowlist UUIDs, global default cap, per-user override map,
  and a catalog of active users (id + display label) so the UI can
  render checkboxes / select menus without a second round-trip.

* ``PUT  /api/settings/assistant/allowlist`` — replace the allowlist
  with the supplied list of user UUIDs.

* ``PUT  /api/settings/assistant/budget`` — set the global default
  cap and replace the per-user override map.

Both writes audit-log with a stable ``settings.assistant.*`` action
prefix so the audit log is filterable.

This module owns no schema — it reads/writes via the existing
``assistant_flag`` and ``assistant.budget`` services, which are the
authoritative homes for the underlying ``cms_settings`` keys.  The
admin UI is therefore the only consumer of these endpoints; the
runtime path (chat router, agent loop) still goes through the
service layer directly.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.services.audit_service import audit_log
from cms.auth import require_permission
from cms.database import get_db
from cms.models.user import User
from cms.services.assistant.budget import (
    DEFAULT_DAILY_TOKEN_CAP,
    get_default_cap,
    get_overrides,
    set_default_cap,
    set_user_override,
    clear_user_override,
)
from cms.services.assistant_flag import get_allowlist, set_allowlist

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/settings/assistant", tags=["assistant-settings"])


# ── Schemas ──────────────────────────────────────────────────────────


class AssistantUserCatalogEntry(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str | None = None
    email: str | None = None


class AssistantSettingsOut(BaseModel):
    allowlist: list[uuid.UUID]
    default_cap: int
    overrides: dict[str, int]
    default_cap_fallback: int = Field(
        default=DEFAULT_DAILY_TOKEN_CAP,
        description=(
            "The compiled-in fallback cap used when no global cap is "
            "configured.  Returned so the UI can show it as the "
            "placeholder."
        ),
    )
    users: list[AssistantUserCatalogEntry]


class AssistantAllowlistIn(BaseModel):
    user_ids: list[uuid.UUID] = Field(default_factory=list)


class AssistantBudgetIn(BaseModel):
    default_cap: int
    overrides: dict[str, int] = Field(default_factory=dict)


# ── Helpers ──────────────────────────────────────────────────────────


async def _load_user_catalog(db: AsyncSession) -> list[AssistantUserCatalogEntry]:
    """All active users, sorted by display label, for the admin UI."""
    rows = (
        await db.execute(
            select(User)
            .where(User.is_active == True)  # noqa: E712
            .order_by(User.username)
        )
    ).scalars().all()
    return [
        AssistantUserCatalogEntry(
            id=u.id,
            username=u.username,
            display_name=u.display_name,
            email=u.email,
        )
        for u in rows
    ]


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("", response_model=AssistantSettingsOut)
async def get_assistant_settings(
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission("settings:write")),
) -> AssistantSettingsOut:
    allowlist = await get_allowlist(db)
    default_cap = await get_default_cap(db)
    overrides_map = await get_overrides(db)
    users = await _load_user_catalog(db)
    return AssistantSettingsOut(
        allowlist=allowlist,
        default_cap=default_cap,
        overrides={str(k): v for k, v in overrides_map.items()},
        users=users,
    )


@router.put("/allowlist", response_model=AssistantSettingsOut)
async def put_assistant_allowlist(
    payload: AssistantAllowlistIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission("settings:write")),
) -> AssistantSettingsOut:
    # Validate every UUID resolves to an actual active user — silently
    # dropping unknown ids would mask UI bugs; refusing the whole call
    # is the more honest behaviour.
    if payload.user_ids:
        known_ids = {
            u.id
            for u in (
                await db.execute(
                    select(User).where(User.id.in_(payload.user_ids))
                )
            ).scalars().all()
        }
        unknown = [str(uid) for uid in payload.user_ids if uid not in known_ids]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Allowlist contains unknown user_ids.",
                    "unknown_user_ids": unknown,
                },
            )

    await set_allowlist(db, payload.user_ids)
    await audit_log(
        db,
        user=_user,
        action="settings.assistant.allowlist.update",
        resource_type="settings",
        description=f"Updated Assistant allowlist ({len(payload.user_ids)} users)",
        details={"user_ids": [str(uid) for uid in payload.user_ids]},
        request=request,
    )
    await db.commit()
    return await get_assistant_settings(db=db, _user=_user)


@router.put("/budget", response_model=AssistantSettingsOut)
async def put_assistant_budget(
    payload: AssistantBudgetIn,
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission("settings:write")),
) -> AssistantSettingsOut:
    # Validate override keys parse as UUIDs of known active users.
    parsed_overrides: dict[uuid.UUID, int] = {}
    bad_keys: list[str] = []
    for k, v in payload.overrides.items():
        try:
            parsed_overrides[uuid.UUID(k)] = int(v)
        except (ValueError, TypeError):
            bad_keys.append(k)
    if bad_keys:
        raise HTTPException(
            status_code=400,
            detail={
                "message": "Override map has invalid entries.",
                "invalid_keys": bad_keys,
            },
        )
    if parsed_overrides:
        known_ids = {
            u.id
            for u in (
                await db.execute(
                    select(User).where(User.id.in_(list(parsed_overrides.keys())))
                )
            ).scalars().all()
        }
        unknown = [str(uid) for uid in parsed_overrides if uid not in known_ids]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail={
                    "message": "Override map references unknown user_ids.",
                    "unknown_user_ids": unknown,
                },
            )

    await set_default_cap(db, payload.default_cap)

    # Reconcile the override map: anything in current-but-not-incoming
    # gets cleared so the UI can edit overrides additively or
    # subtractively in one round-trip.
    current = await get_overrides(db)
    incoming_ids = set(parsed_overrides.keys())
    for uid in list(current.keys()):
        if uid not in incoming_ids:
            await clear_user_override(db, uid)
    for uid, cap in parsed_overrides.items():
        await set_user_override(db, uid, cap)

    await audit_log(
        db,
        user=_user,
        action="settings.assistant.budget.update",
        resource_type="settings",
        description=(
            f"Updated Assistant budget (default_cap={payload.default_cap}, "
            f"overrides={len(parsed_overrides)})"
        ),
        details={
            "default_cap": payload.default_cap,
            "overrides": {str(uid): cap for uid, cap in parsed_overrides.items()},
        },
        request=request,
    )
    await db.commit()
    return await get_assistant_settings(db=db, _user=_user)
