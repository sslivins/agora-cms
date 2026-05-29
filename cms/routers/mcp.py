"""MCP server auth API — used by the MCP container to validate bearer tokens."""

import uuid as _uuid

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import (
    SERVICE_KEY_PREFIX,
    SETTING_MCP_ENABLED,
    SETTING_MCP_SERVICE_KEY_HASH,
    _hash_api_key,
    _resolve_user_from_api_key,
    compute_effective_permissions,
    get_setting,
)
from cms.database import get_db
from cms.models.user import User

router = APIRouter(prefix="/api/mcp")


async def _resolve_on_behalf_of_user(
    on_behalf_of: str, db: AsyncSession
) -> User:
    """Resolve ``X-On-Behalf-Of`` to an active :class:`User`.

    Validates that ``on_behalf_of`` parses as a UUID, that the user
    exists, and that the user is active.  Raises HTTPException with the
    appropriate status on any failure — never returns ``None``.

    The header is treated strictly as "which DB row to load" — the
    caller's permissions come from that user's actual ``role``, NOT
    from any claim in the header.  This means a compromised CMS could
    impersonate any user (same blast radius as the existing service-key
    auth path used by the MCP back-channel calls) but cannot forge
    permissions a user doesn't have.
    """
    try:
        user_id = _uuid.UUID(on_behalf_of)
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=400,
            detail="X-On-Behalf-Of must be a valid user UUID",
        )

    result = await db.execute(
        select(User).options(selectinload(User.role)).where(User.id == user_id)
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(status_code=404, detail="On-behalf-of user not found")
    if not user.is_active:
        raise HTTPException(
            status_code=403, detail="On-behalf-of user is disabled"
        )
    return user


@router.get("/auth")
async def verify_mcp_token(
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(default=""),
    x_on_behalf_of: str | None = Header(default=None),
):
    """Validate a bearer token and return the resolved user's permissions.

    Called by the MCP server's ``BearerAuthMiddleware`` on every
    incoming connection.  Supports two authentication modes:

    1. **Personal MCP key** (existing path).  The bearer token is a
       ``key_type="mcp"`` API key owned by a user; the response carries
       that user's effective permissions.

    2. **Service key + X-On-Behalf-Of** (new — used by the in-process
       Assistant agent).  The bearer is the CMS-issued MCP service key
       (``agora_svc_...``) AND the ``X-On-Behalf-Of`` header carries
       the UUID of the user the request is acting for.  The response
       carries THAT user's effective permissions — never a synthetic
       "service" permission set.  This lets the CMS act as a trusted
       proxy without duplicating per-user secrets while keeping the
       per-tool permission gating identical to mode (1).

    Both modes share the same ``compute_effective_permissions`` path
    (user role ∩ MCP role ceiling).  No new privileged permission set
    is introduced.
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    enabled = await get_setting(db, SETTING_MCP_ENABLED)
    if enabled != "true":
        raise HTTPException(status_code=403, detail="MCP server is disabled")

    # ── Mode 2: service key + X-On-Behalf-Of ──────────────────────────
    if token.startswith(SERVICE_KEY_PREFIX):
        service_key_hash = await get_setting(db, SETTING_MCP_SERVICE_KEY_HASH)
        if not service_key_hash or _hash_api_key(token) != service_key_hash:
            raise HTTPException(status_code=401, detail="Invalid service key")
        if not x_on_behalf_of:
            raise HTTPException(
                status_code=400,
                detail="Service-key auth requires the X-On-Behalf-Of header.",
            )
        user = await _resolve_on_behalf_of_user(x_on_behalf_of, db)
        permissions = await compute_effective_permissions(user, "mcp", db)
        return {
            "valid": True,
            "user": user.display_name or user.email or user.username,
            "role": user.role.name if user.role else None,
            "key_type": "service",
            "on_behalf_of": str(user.id),
            "permissions": permissions,
        }

    # ── Mode 1: personal MCP key (existing path) ──────────────────────
    result = await _resolve_user_from_api_key(token, db)
    if result is None:
        raise HTTPException(status_code=401, detail="Invalid API key")

    user, key_row = result

    # Enforce key type — only MCP keys are accepted here
    if key_row.key_type != "mcp":
        raise HTTPException(
            status_code=403,
            detail="Only MCP keys can be used with the MCP server. "
                   "Create an MCP key in your profile.",
        )

    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")

    # Compute effective permissions (user role ∩ MCP role ceiling)
    permissions = await compute_effective_permissions(user, "mcp", db)

    return {
        "valid": True,
        "user": user.display_name or user.email or user.username,
        "role": user.role.name if user.role else None,
        "key_type": key_row.key_type,
        "permissions": permissions,
    }
