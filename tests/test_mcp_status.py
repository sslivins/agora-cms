"""Tests for MCP status endpoint including API connection health check."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _mock_httpx_client(responses):
    """Create a mock httpx.AsyncClient that returns responses in order."""
    mock_instance = AsyncMock()
    mock_instance.get = AsyncMock(side_effect=responses)

    mock_cm = MagicMock()
    mock_cm.__aenter__ = AsyncMock(return_value=mock_instance)
    mock_cm.__aexit__ = AsyncMock(return_value=False)
    return mock_cm


@pytest.mark.asyncio
class TestMCPStatus:
    """Test /api/mcp/status endpoint."""

    async def test_mcp_offline(self, client):
        """When MCP container is unreachable, should return online=False."""
        mock_cm = _mock_httpx_client([Exception("Connection refused")])
        with patch("httpx.AsyncClient", return_value=mock_cm):
            resp = await client.get("/api/mcp/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["online"] is False
            assert data["api_connected"] is False

    async def test_mcp_online_api_connected(self, client):
        """When MCP is online and API check passes, both should be True."""
        health_resp = MagicMock()
        health_resp.status_code = 200
        api_resp = MagicMock()
        api_resp.status_code = 200
        api_resp.json.return_value = {"status": "ok", "device_count": 2}

        mock_cm = _mock_httpx_client([health_resp, api_resp])
        with patch("httpx.AsyncClient", return_value=mock_cm):
            resp = await client.get("/api/mcp/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["online"] is True
            assert data["api_connected"] is True

    async def test_mcp_online_api_auth_failed(self, client):
        """When MCP is online but API key is invalid, api_connected should be False."""
        health_resp = MagicMock()
        health_resp.status_code = 200
        api_resp = MagicMock()
        api_resp.status_code = 502
        api_resp.json.return_value = {"status": "error", "detail": "401 Unauthorized"}

        mock_cm = _mock_httpx_client([health_resp, api_resp])
        with patch("httpx.AsyncClient", return_value=mock_cm):
            resp = await client.get("/api/mcp/status")
            assert resp.status_code == 200
            data = resp.json()
            assert data["online"] is True
            assert data["api_connected"] is False
            assert "api_error" in data
