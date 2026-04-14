"""MCP server auth API — used by the MCP container to validate bearer tokens."""

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import (
    SETTING_MCP_ENABLED,
    _resolve_user_from_api_key,
    compute_effective_permissions,
    get_setting,
)
from cms.database import get_db

router = APIRouter(prefix="/api/mcp")


@router.get("/auth")
async def verify_mcp_token(
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(default=""),
):
    """Validate a bearer token (user API key) and return user permissions.

    Called by the MCP server on each incoming connection.
    The bearer token must be a valid user API key (``agora_...``) with
    ``key_type="mcp"``.  API-type keys are rejected.
    Returns the user's display name, role, and effective permissions
    (user role ∩ MCP role ceiling).
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    enabled = await get_setting(db, SETTING_MCP_ENABLED)
    if enabled != "true":
        raise HTTPException(status_code=403, detail="MCP server is disabled")

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
