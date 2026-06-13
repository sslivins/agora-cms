"""Regression tests for ``_call_api`` error surfacing in mcp/server.py.

Before this fix a non-401 ``HTTPStatusError`` propagated as a bare
``"400 Bad Request"`` and the assistant LLM (and therefore the user)
never saw the CMS-provided ``detail`` (e.g. the slideshow ACL message
"A global slideshow can only reference global source assets …").
These tests pin that the JSON ``detail`` is now re-raised verbatim,
with sensible fallbacks, while the 401 reload-and-retry path is
preserved.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


@pytest.fixture
def mcp_server_module():
    """Import mcp/server.py as a standalone module with mocked MCP SDK."""
    saved_modules = {}
    for mod_name in [
        "mcp",
        "mcp.server",
        "mcp.server.fastmcp",
        "mcp.server.transport_security",
        "cms_client",
    ]:
        saved_modules[mod_name] = sys.modules.get(mod_name)
        mock_mod = types.ModuleType(mod_name)
        if mod_name == "mcp.server.fastmcp":
            mock_mod.FastMCP = MagicMock()
        if mod_name == "mcp.server.transport_security":
            mock_mod.TransportSecuritySettings = MagicMock()
        if mod_name == "cms_client":
            mock_mod.CMSClient = MagicMock()
        sys.modules[mod_name] = mock_mod

    server_path = Path(__file__).resolve().parent.parent / "mcp" / "server.py"
    spec = importlib.util.spec_from_file_location("mcp_server_mod", server_path)
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        yield mod
    finally:
        for mod_name, original in saved_modules.items():
            if original is None:
                sys.modules.pop(mod_name, None)
            else:
                sys.modules[mod_name] = original


def _status_error(status: int, *, json_body=None, text: str = "") -> httpx.HTTPStatusError:
    request = httpx.Request("PUT", "http://cms/api/assets/x/slides")
    if json_body is not None:
        response = httpx.Response(status, json=json_body, request=request)
    else:
        response = httpx.Response(status, text=text, request=request)
    return httpx.HTTPStatusError("err", request=request, response=response)


def _client_raising(exc: Exception) -> MagicMock:
    client = MagicMock()
    client.set_slideshow_slides = AsyncMock(side_effect=exc)
    return client


@pytest.mark.asyncio
async def test_call_api_surfaces_400_detail(mcp_server_module, monkeypatch):
    mod = mcp_server_module
    msg = (
        "A global slideshow can only reference global source assets. "
        "Not global: mine.png. Mark these global first."
    )
    client = _client_raising(_status_error(400, json_body={"detail": msg}))
    monkeypatch.setattr(mod, "_get_client", lambda: client)

    with pytest.raises(RuntimeError) as ei:
        await mod._call_api("set_slideshow_slides", "asset-id", [])
    assert msg in str(ei.value)
    assert "400" in str(ei.value)


@pytest.mark.asyncio
async def test_call_api_falls_back_to_text_when_no_detail(
    mcp_server_module, monkeypatch
):
    mod = mcp_server_module
    client = _client_raising(_status_error(422, text="Unprocessable thing"))
    monkeypatch.setattr(mod, "_get_client", lambda: client)

    with pytest.raises(RuntimeError) as ei:
        await mod._call_api("set_slideshow_slides", "asset-id", [])
    assert "Unprocessable thing" in str(ei.value)
    assert "422" in str(ei.value)


@pytest.mark.asyncio
async def test_call_api_401_reloads_key_and_retries(mcp_server_module, monkeypatch):
    mod = mcp_server_module

    # First client raises 401; second (post-reload) client succeeds.
    bad = _client_raising(_status_error(401, json_body={"detail": "stale key"}))
    good = MagicMock()
    good.set_slideshow_slides = AsyncMock(return_value={"ok": True})
    clients = iter([bad, good])
    monkeypatch.setattr(mod, "_get_client", lambda: next(clients))
    monkeypatch.setattr(mod, "_reload_service_key", AsyncMock())

    result = await mod._call_api("set_slideshow_slides", "asset-id", [])
    assert result == {"ok": True}
    mod._reload_service_key.assert_awaited_once()
