"""Tests for MCP auto-rotation loop and self-healing 401 fallback."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


# ── Auto-rotation loop tests ──────────────────────────────────────────────


class TestServiceKeyRotationLoop:
    """Tests for cms.main.service_key_rotation_loop."""

    @pytest.fixture
    def mock_deps(self):
        """Patch all rotation loop dependencies."""
        with (
            patch("cms.main.asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("cms.auth.get_setting", new_callable=AsyncMock) as mock_get_setting,
            patch("cms.auth.provision_service_key", new_callable=AsyncMock) as mock_provision,
            patch("cms.mcp_utils.notify_mcp_reload", new_callable=AsyncMock) as mock_notify,
            patch("cms.auth.get_settings") as mock_get_settings,
        ):
            mock_get_settings.return_value = MagicMock(
                service_key_path="/tmp/test-key",
                azure_keyvault_uri=None,
                mcp_server_url="http://mcp:8000",
            )
            mock_provision.return_value = ("raw-key-abc", "rka")
            mock_notify.return_value = True

            yield {
                "sleep": mock_sleep,
                "get_setting": mock_get_setting,
                "provision": mock_provision,
                "notify": mock_notify,
                "get_settings": mock_get_settings,
            }

    @pytest.mark.asyncio
    async def test_rotation_calls_provision_and_notify(self, mock_deps):
        """When MCP is enabled and key exists, rotation provisions + notifies."""
        call_count = 0

        async def sleep_side_effect(seconds):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return  # initial 60s startup delay
            raise asyncio.CancelledError()  # stop after first rotation

        mock_deps["sleep"].side_effect = sleep_side_effect
        mock_deps["get_setting"].side_effect = lambda db, key: {
            "mcp_enabled": "true",
            "mcp_service_key_hash": "some-hash",
        }.get(key)

        mock_db = AsyncMock()

        async def fake_get_db():
            yield mock_db

        with patch("cms.database.get_db", fake_get_db):
            from cms.main import service_key_rotation_loop
            await service_key_rotation_loop()

        mock_deps["provision"].assert_awaited_once()
        mock_deps["notify"].assert_awaited_once()

    @pytest.mark.asyncio
    async def test_rotation_skips_when_mcp_disabled(self, mock_deps):
        """When MCP is not enabled, rotation does nothing."""
        call_count = 0

        async def sleep_side_effect(seconds):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return
            raise asyncio.CancelledError()

        mock_deps["sleep"].side_effect = sleep_side_effect
        mock_deps["get_setting"].side_effect = lambda db, key: {
            "mcp_enabled": "false",
            "mcp_service_key_hash": "some-hash",
        }.get(key)

        mock_db = AsyncMock()

        async def fake_get_db():
            yield mock_db

        with patch("cms.database.get_db", fake_get_db):
            from cms.main import service_key_rotation_loop
            await service_key_rotation_loop()

        mock_deps["provision"].assert_not_awaited()
        mock_deps["notify"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotation_skips_when_no_key_hash(self, mock_deps):
        """When no key hash exists (MCP never enabled), rotation skips."""
        call_count = 0

        async def sleep_side_effect(seconds):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return
            raise asyncio.CancelledError()

        mock_deps["sleep"].side_effect = sleep_side_effect
        mock_deps["get_setting"].side_effect = lambda db, key: {
            "mcp_enabled": "true",
            "mcp_service_key_hash": None,
        }.get(key)

        mock_db = AsyncMock()

        async def fake_get_db():
            yield mock_db

        with patch("cms.database.get_db", fake_get_db):
            from cms.main import service_key_rotation_loop
            await service_key_rotation_loop()

        mock_deps["provision"].assert_not_awaited()
        mock_deps["notify"].assert_not_awaited()

    @pytest.mark.asyncio
    async def test_rotation_cancels_cleanly_during_startup(self, mock_deps):
        """CancelledError during startup sleep exits gracefully."""
        mock_deps["sleep"].side_effect = asyncio.CancelledError()

        from cms.main import service_key_rotation_loop

        await service_key_rotation_loop()

        mock_deps["provision"].assert_not_awaited()


# ── MCP _call_api fallback tests ──────────────────────────────────────────


class TestCallApiFallback:
    """Tests for mcp.server._call_api self-healing on 401."""

    @pytest.fixture
    def mcp_module(self):
        """Import mcp/server.py with mocked MCP SDK (reuses test_mcp_reload_key pattern)."""
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
        spec = importlib.util.spec_from_file_location("mcp_server_mod_rotate", server_path)
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

    @pytest.mark.asyncio
    async def test_call_api_retries_on_401(self, mcp_module):
        """_call_api reloads key and retries on 401."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        call_count = 0

        async def mock_method(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise httpx.HTTPStatusError(
                    "Unauthorized", request=MagicMock(), response=mock_response
                )
            return {"id": "123", "name": "test"}

        mock_client = MagicMock()
        mock_client.list_devices = mock_method
        mock_reload = AsyncMock()

        with (
            patch.object(mcp_module, "_get_client", return_value=mock_client),
            patch.object(mcp_module, "_reload_service_key", mock_reload),
        ):
            result = await mcp_module._call_api("list_devices")
            assert result == {"id": "123", "name": "test"}
            assert call_count == 2
            mock_reload.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_call_api_propagates_non_401(self, mcp_module):
        """_call_api does NOT retry on non-401 errors."""
        mock_response = MagicMock()
        mock_response.status_code = 500

        async def mock_method(*args, **kwargs):
            raise httpx.HTTPStatusError(
                "Server Error", request=MagicMock(), response=mock_response
            )

        mock_client = MagicMock()
        mock_client.list_devices = mock_method
        mock_reload = AsyncMock()

        with (
            patch.object(mcp_module, "_get_client", return_value=mock_client),
            patch.object(mcp_module, "_reload_service_key", mock_reload),
        ):
            with pytest.raises(httpx.HTTPStatusError) as exc_info:
                await mcp_module._call_api("list_devices")
            assert exc_info.value.response.status_code == 500
            mock_reload.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_call_api_fails_on_double_401(self, mcp_module):
        """_call_api retries once — if still 401 after reload, propagates."""
        mock_response = MagicMock()
        mock_response.status_code = 401

        async def mock_method(*args, **kwargs):
            raise httpx.HTTPStatusError(
                "Unauthorized", request=MagicMock(), response=mock_response
            )

        mock_client = MagicMock()
        mock_client.list_devices = mock_method
        mock_reload = AsyncMock()

        with (
            patch.object(mcp_module, "_get_client", return_value=mock_client),
            patch.object(mcp_module, "_reload_service_key", mock_reload),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await mcp_module._call_api("list_devices")
            mock_reload.assert_awaited_once()

