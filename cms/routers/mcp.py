"""MCP server auth API — used by the MCP container to validate bearer tokens."""

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import SETTING_MCP_API_KEY, SETTING_MCP_ENABLED, get_setting
from cms.database import get_db

router = APIRouter(prefix="/api/mcp")


@router.get("/auth")
async def verify_mcp_token(
    db: AsyncSession = Depends(get_db),
    authorization: str = Header(default=""),
):
    """Validate a bearer token and check if MCP is enabled.

    Called by the MCP server on each incoming connection.
    Returns {"valid": true, "role": "admin"} on success.
    """
    token = authorization.removeprefix("Bearer ").strip()
    if not token:
        raise HTTPException(status_code=401, detail="Missing bearer token")

    enabled = await get_setting(db, SETTING_MCP_ENABLED)
    if enabled != "true":
        raise HTTPException(status_code=403, detail="MCP server is disabled")

    stored_key = await get_setting(db, SETTING_MCP_API_KEY)
    if not stored_key or token != stored_key:
        raise HTTPException(status_code=401, detail="Invalid API key")

    return {"valid": True, "role": "admin"}
