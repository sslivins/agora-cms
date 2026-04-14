"""MCP utility helpers — shared between UI routes and background tasks."""

import logging

import httpx

from cms.config import Settings

logger = logging.getLogger(__name__)


async def notify_mcp_reload(settings: Settings) -> bool:
    """Tell the MCP server to reload its service key from Key Vault/file.

    Returns True if the MCP acknowledged the reload, False otherwise.
    Fire-and-forget — failures are logged but never raise.
    """
    mcp_url = settings.mcp_server_url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.post(f"{mcp_url}/reload-key")
            if resp.status_code == 200:
                logger.info("MCP service key reload triggered")
                return True
            else:
                logger.warning("MCP reload-key returned %s", resp.status_code)
                return False
    except Exception as exc:
        logger.warning("Failed to notify MCP of key reload: %s", exc)
        return False
