"""Admin API for the in-CMS Assistant settings.

Two endpoints, both gated on ``settings:write``:

* ``GET  /api/settings/assistant`` — state for the admin UI: the global
  default token cap, the per-user override map, and a catalog of active
  users (id + display label) so the UI can render the override rows
  without a second round-trip.

* ``PUT  /api/settings/assistant/budget`` — set the global default cap
  and replace the per-user override map.

Writes audit-log with a stable ``settings.assistant.*`` action prefix so
the audit log is filterable.

*Who* may use the Assistant is no longer set here.  That is a feature
flag like any other and lives on the Features tab
(``cms/routers/features.py``); this module is only about spend.

This module owns no schema — it reads/writes via the existing
``assistant.budget`` service, which is the authoritative home for the
underlying ``cms_settings`` keys.  The admin UI is therefore the only
consumer of these endpoints; the runtime path (chat router, agent loop)
still goes through the service layer directly.
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

logger = logging.getLogger(__name__)


router = APIRouter(prefix="/api/settings/assistant", tags=["assistant-settings"])


# ── Schemas ──────────────────────────────────────────────────────────


class AssistantUserCatalogEntry(BaseModel):
    id: uuid.UUID
    username: str
    display_name: str | None = None
    email: str | None = None


class AssistantSettingsOut(BaseModel):
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
    default_cap = await get_default_cap(db)
    overrides_map = await get_overrides(db)
    users = await _load_user_catalog(db)
    return AssistantSettingsOut(
        default_cap=default_cap,
        overrides={str(k): v for k, v in overrides_map.items()},
        users=users,
    )


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
