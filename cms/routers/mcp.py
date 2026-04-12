"""MCP server auth API — used by the MCP container to validate bearer tokens."""

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import SETTING_MCP_ENABLED, _resolve_user_from_api_key, get_setting
from cms.database import get_db

router = APIRouter(prefix="/api/mcp")


@router.get("/auth")
async def verify_mcp_token(
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(default=""),
):
    """Validate a bearer token (user API key) and return user permissions.

    Called by the MCP server on each incoming connection.
    The bearer token must be a valid user API key (``agora_...``).
    Returns the user's display name, role, and permissions list.
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    enabled = await get_setting(db, SETTING_MCP_ENABLED)
    if enabled != "true":
        raise HTTPException(status_code=403, detail="MCP server is disabled")

    user = await _resolve_user_from_api_key(token, db)
    if user is None:
        raise HTTPException(status_code=401, detail="Invalid API key")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User account is disabled")

    permissions = user.role.permissions if user.role else []

    return {
        "valid": True,
        "user": user.display_name or user.email or user.username,
        "role": user.role.name if user.role else None,
        "permissions": permissions,
    }
