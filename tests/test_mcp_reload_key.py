"""Tests for push-based MCP service key reload.

Tests cover:
- MCP /reload-key endpoint (POST triggers reload, wrong method → 405)
- CMS _notify_mcp_reload helper (fire-and-forget, graceful error handling)
- CMS endpoints (toggle, regenerate) call _notify_mcp_reload after key changes
- Watcher removal: SERVICE_KEY_RELOAD_INTERVAL and _service_key_watcher are gone
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import types
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

VAULT_URI = "https://test-vault.vault.azure.net/"


# ── Fixture: import mcp/server.py as a standalone module ──


@pytest.fixture
def mcp_server_module(tmp_path):
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


# ── MCP-side: /reload-key endpoint ──


class TestReloadKeyEndpoint:
    @pytest.mark.asyncio
    async def test_post_reloads_key_and_returns_status(self, mcp_server_module):
        """POST /reload-key should trigger _reload_service_key and return JSON."""
        mod = mcp_server_module

        # Pre-set a key so has_key is True after reload
        key_file = Path(__file__).parent / "_tmp_svc.key"
        try:
            key_file.write_text("test_key_for_reload")
            mod.SERVICE_KEY_PATH = str(key_file)
            mod.AZURE_KEYVAULT_URI = ""
            os.environ.pop("SERVICE_KEY", None)

            from starlette.testclient import TestClient
            from starlette.applications import Starlette
            from starlette.routing import Route

            test_app = Starlette(
                routes=[Route("/reload-key", mod.reload_key_endpoint, methods=["POST"])],
            )
            with TestClient(test_app) as client:
                resp = client.post("/reload-key")
                assert resp.status_code == 200
                data = resp.json()
                assert data["reloaded"] is True
                assert data["has_key"] is True
                # Key should now be loaded
                assert mod._service_key == "test_key_for_reload"
        finally:
            key_file.unlink(missing_ok=True)

    @pytest.mark.asyncio
    async def test_reload_with_no_key_returns_has_key_false(self, mcp_server_module):
        """When no key source is available, has_key should be False."""
        mod = mcp_server_module
        mod.SERVICE_KEY_PATH = "/nonexistent/path.key"
        mod.AZURE_KEYVAULT_URI = ""
        os.environ.pop("SERVICE_KEY", None)

        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route

        test_app = Starlette(
            routes=[Route("/reload-key", mod.reload_key_endpoint, methods=["POST"])],
        )
        with TestClient(test_app) as client:
            resp = client.post("/reload-key")
            assert resp.status_code == 200
            data = resp.json()
            assert data["reloaded"] is True
            assert data["has_key"] is False

    @pytest.mark.asyncio
    async def test_reload_updates_key_from_keyvault(self, mcp_server_module):
        """Reload should pick up a new key from Key Vault."""
        mod = mcp_server_module
        mod.AZURE_KEYVAULT_URI = VAULT_URI
        mod.SERVICE_KEY_PATH = "/nonexistent/path.key"
        mod._service_key = "old_key"
        os.environ.pop("SERVICE_KEY", None)

        with patch.object(mod, "_read_key_from_keyvault", return_value="new_kv_key"):
            from starlette.testclient import TestClient
            from starlette.applications import Starlette
            from starlette.routing import Route

            test_app = Starlette(
                routes=[Route("/reload-key", mod.reload_key_endpoint, methods=["POST"])],
            )
            with TestClient(test_app) as client:
                resp = client.post("/reload-key")
                assert resp.status_code == 200
                assert mod._service_key == "new_kv_key"


class TestReloadKeyBypassesAuth:
    """Ensure /reload-key is exempt from BearerAuthMiddleware."""

    @pytest.mark.asyncio
    async def test_reload_key_does_not_require_auth(self, mcp_server_module, tmp_path):
        """POST /reload-key without Authorization header should return 200, not 401."""
        mod = mcp_server_module

        # Provide a key file so reload has something to load
        key_file = tmp_path / "svc.key"
        key_file.write_text("test_key")
        mod.SERVICE_KEY_PATH = str(key_file)
        mod.AZURE_KEYVAULT_URI = ""
        os.environ.pop("SERVICE_KEY", None)

        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.routing import Route
        from starlette.testclient import TestClient

        # Build app WITH the auth middleware — same as production
        app = Starlette(
            routes=[
                Route("/health", mod.health_endpoint),
                Route("/reload-key", mod.reload_key_endpoint, methods=["POST"]),
            ],
            middleware=[Middleware(mod.BearerAuthMiddleware)],
        )
        with TestClient(app) as client:
            # No Authorization header — must still succeed
            resp = client.post("/reload-key")
            assert resp.status_code == 200, f"Expected 200 but got {resp.status_code}: {resp.text}"
            assert resp.json()["reloaded"] is True

    @pytest.mark.asyncio
    async def test_other_routes_still_require_auth(self, mcp_server_module):
        """Non-exempt routes should still return 401 without a token."""
        mod = mcp_server_module

        from starlette.applications import Starlette
        from starlette.middleware import Middleware
        from starlette.responses import JSONResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        async def dummy_endpoint(request):
            return JSONResponse({"ok": True})

        app = Starlette(
            routes=[
                Route("/health", mod.health_endpoint),
                Route("/reload-key", mod.reload_key_endpoint, methods=["POST"]),
                Route("/some-protected", dummy_endpoint),
            ],
            middleware=[Middleware(mod.BearerAuthMiddleware)],
        )
        with TestClient(app) as client:
            resp = client.get("/some-protected")
            assert resp.status_code == 401


# ── Docker Compose scenario: file-based key reload ──


class TestFileBasedReload:
    """Simulate Docker Compose: key exchanged via shared volume file, no Key Vault."""

    @pytest.mark.asyncio
    async def test_reload_picks_up_new_key_from_file(self, mcp_server_module, tmp_path):
        """After CMS writes a new key to the shared file, /reload-key picks it up."""
        mod = mcp_server_module
        key_file = tmp_path / "mcp-service.key"
        key_file.write_text("original_key_abc123")
        mod.SERVICE_KEY_PATH = str(key_file)
        mod.AZURE_KEYVAULT_URI = ""
        os.environ.pop("SERVICE_KEY", None)

        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route

        test_app = Starlette(
            routes=[Route("/reload-key", mod.reload_key_endpoint, methods=["POST"])],
        )
        with TestClient(test_app) as client:
            # Initial load
            resp = client.post("/reload-key")
            assert resp.status_code == 200
            assert mod._service_key == "original_key_abc123"

            # CMS regenerates — writes new key to shared volume
            key_file.write_text("regenerated_key_xyz789")

            # Push reload signal
            resp = client.post("/reload-key")
            assert resp.status_code == 200
            assert resp.json()["reloaded"] is True
            assert resp.json()["has_key"] is True
            assert mod._service_key == "regenerated_key_xyz789"

    @pytest.mark.asyncio
    async def test_reload_detects_revoked_key(self, mcp_server_module, tmp_path):
        """After CMS revokes (deletes file), /reload-key reflects has_key=False."""
        mod = mcp_server_module
        key_file = tmp_path / "mcp-service.key"
        key_file.write_text("active_key_111")
        mod.SERVICE_KEY_PATH = str(key_file)
        mod.AZURE_KEYVAULT_URI = ""
        os.environ.pop("SERVICE_KEY", None)

        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route

        test_app = Starlette(
            routes=[Route("/reload-key", mod.reload_key_endpoint, methods=["POST"])],
        )
        with TestClient(test_app) as client:
            # Load active key
            resp = client.post("/reload-key")
            assert mod._service_key == "active_key_111"

            # CMS revokes — deletes the key file
            key_file.unlink()

            # Push reload signal
            resp = client.post("/reload-key")
            assert resp.status_code == 200
            assert resp.json()["has_key"] is False
            assert mod._service_key == ""

    @pytest.mark.asyncio
    async def test_reload_ignores_unchanged_file(self, mcp_server_module, tmp_path):
        """If the file hasn't changed, key stays the same (no unnecessary update)."""
        mod = mcp_server_module
        key_file = tmp_path / "mcp-service.key"
        key_file.write_text("stable_key_999")
        mod.SERVICE_KEY_PATH = str(key_file)
        mod.AZURE_KEYVAULT_URI = ""
        os.environ.pop("SERVICE_KEY", None)

        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route

        test_app = Starlette(
            routes=[Route("/reload-key", mod.reload_key_endpoint, methods=["POST"])],
        )
        with TestClient(test_app) as client:
            client.post("/reload-key")
            assert mod._service_key == "stable_key_999"

            # Reload again — file unchanged
            resp = client.post("/reload-key")
            assert resp.status_code == 200
            assert mod._service_key == "stable_key_999"


class TestEndToEndDockerCompose:
    """Simulate full CMS → file → MCP reload cycle as in Docker Compose."""

    @pytest.mark.asyncio
    async def test_provision_write_file_reload_matches(self, db_session, mcp_server_module, tmp_path):
        """CMS provisions key → writes to file → MCP reloads → keys match."""
        mod = mcp_server_module
        key_file = tmp_path / "mcp-service.key"
        mod.SERVICE_KEY_PATH = str(key_file)
        mod.AZURE_KEYVAULT_URI = ""
        os.environ.pop("SERVICE_KEY", None)

        from cms.auth import provision_service_key

        raw_key, prefix = await provision_service_key(db_session, str(key_file))

        # The key file should exist now
        assert key_file.exists()
        file_content = key_file.read_text().strip()
        assert file_content == raw_key

        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route

        test_app = Starlette(
            routes=[Route("/reload-key", mod.reload_key_endpoint, methods=["POST"])],
        )
        with TestClient(test_app) as client:
            resp = client.post("/reload-key")
            assert resp.status_code == 200
            assert mod._service_key == raw_key

    @pytest.mark.asyncio
    async def test_regenerate_updates_file_reload_picks_up(self, db_session, mcp_server_module, tmp_path):
        """CMS regenerates key → file updated → MCP reload picks up new key."""
        mod = mcp_server_module
        key_file = tmp_path / "mcp-service.key"
        mod.SERVICE_KEY_PATH = str(key_file)
        mod.AZURE_KEYVAULT_URI = ""
        os.environ.pop("SERVICE_KEY", None)

        from cms.auth import provision_service_key

        # First provision
        key1, _ = await provision_service_key(db_session, str(key_file))

        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route

        test_app = Starlette(
            routes=[Route("/reload-key", mod.reload_key_endpoint, methods=["POST"])],
        )
        with TestClient(test_app) as client:
            client.post("/reload-key")
            assert mod._service_key == key1

            # Regenerate
            key2, _ = await provision_service_key(db_session, str(key_file))
            assert key1 != key2

            # Reload picks up new key
            resp = client.post("/reload-key")
            assert resp.status_code == 200
            assert mod._service_key == key2

    @pytest.mark.asyncio
    async def test_revoke_clears_file_reload_clears_key(self, db_session, mcp_server_module, tmp_path):
        """CMS revokes key → file cleared → MCP reload clears its key."""
        mod = mcp_server_module
        key_file = tmp_path / "mcp-service.key"
        mod.SERVICE_KEY_PATH = str(key_file)
        mod.AZURE_KEYVAULT_URI = ""
        os.environ.pop("SERVICE_KEY", None)

        from cms.auth import provision_service_key, revoke_service_key

        raw_key, _ = await provision_service_key(db_session, str(key_file))

        from starlette.testclient import TestClient
        from starlette.applications import Starlette
        from starlette.routing import Route

        test_app = Starlette(
            routes=[Route("/reload-key", mod.reload_key_endpoint, methods=["POST"])],
        )
        with TestClient(test_app) as client:
            client.post("/reload-key")
            assert mod._service_key == raw_key

            # Revoke
            await revoke_service_key(db_session, str(key_file))

            # Reload should clear the key
            resp = client.post("/reload-key")
            assert resp.status_code == 200
            assert resp.json()["has_key"] is False
            assert mod._service_key == ""


# ── Watcher removal verification ──


class TestWatcherRemoval:
    def test_no_service_key_reload_interval(self, mcp_server_module):
        """SERVICE_KEY_RELOAD_INTERVAL should not exist after PR."""
        mod = mcp_server_module
        assert not hasattr(mod, "SERVICE_KEY_RELOAD_INTERVAL")

    def test_no_service_key_watcher(self, mcp_server_module):
        """_service_key_watcher should not exist after PR."""
        mod = mcp_server_module
        assert not hasattr(mod, "_service_key_watcher")


# ── CMS-side: _notify_mcp_reload helper ──


class TestNotifyMcpReload:
    @pytest.mark.asyncio
    async def test_notify_posts_to_reload_key(self):
        """Should POST to {mcp_url}/reload-key."""
        from cms.ui import _notify_mcp_reload

        settings = MagicMock()
        settings.mcp_server_url = "http://mcp:8000"

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("cms.ui._httpx.AsyncClient", return_value=mock_client):
            await _notify_mcp_reload(settings)

        mock_client.post.assert_called_once_with("http://mcp:8000/reload-key")

    @pytest.mark.asyncio
    async def test_notify_strips_trailing_slash(self):
        """Should strip trailing slash from mcp_server_url."""
        from cms.ui import _notify_mcp_reload

        settings = MagicMock()
        settings.mcp_server_url = "http://mcp:8000/"

        mock_resp = MagicMock()
        mock_resp.status_code = 200

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("cms.ui._httpx.AsyncClient", return_value=mock_client):
            await _notify_mcp_reload(settings)

        mock_client.post.assert_called_once_with("http://mcp:8000/reload-key")

    @pytest.mark.asyncio
    async def test_notify_handles_connection_error_gracefully(self):
        """Connection error should be logged, not raised."""
        from cms.ui import _notify_mcp_reload

        settings = MagicMock()
        settings.mcp_server_url = "http://mcp:8000"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=ConnectionError("MCP unreachable"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("cms.ui._httpx.AsyncClient", return_value=mock_client):
            # Should NOT raise
            await _notify_mcp_reload(settings)

    @pytest.mark.asyncio
    async def test_notify_handles_timeout_gracefully(self):
        """Timeout should be logged, not raised."""
        import httpx
        from cms.ui import _notify_mcp_reload

        settings = MagicMock()
        settings.mcp_server_url = "http://mcp:8000"

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(side_effect=httpx.TimeoutException("timed out"))
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with patch("cms.ui._httpx.AsyncClient", return_value=mock_client):
            await _notify_mcp_reload(settings)

    @pytest.mark.asyncio
    async def test_notify_logs_warning_on_non_200(self):
        """Non-200 response should log a warning."""
        from cms.ui import _notify_mcp_reload

        settings = MagicMock()
        settings.mcp_server_url = "http://mcp:8000"

        mock_resp = MagicMock()
        mock_resp.status_code = 500

        mock_client = AsyncMock()
        mock_client.post = AsyncMock(return_value=mock_resp)
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)

        with (
            patch("cms.ui._httpx.AsyncClient", return_value=mock_client),
            patch("cms.ui._ui_logger") as mock_logger,
        ):
            await _notify_mcp_reload(settings)
            mock_logger.warning.assert_called_once()
            assert "500" in str(mock_logger.warning.call_args)


# ── CMS endpoints call _notify_mcp_reload ──


@pytest.mark.asyncio
class TestEndpointsCallNotify:
    async def test_regenerate_calls_notify(self, app, client):
        """Regenerate endpoint should call _notify_mcp_reload."""
        with patch("cms.ui._notify_mcp_reload", new_callable=AsyncMock) as mock_notify:
            resp = await client.post("/api/mcp/service-key/regenerate")
            assert resp.status_code == 200
            assert resp.json()["regenerated"] is True
            mock_notify.assert_called_once()

    async def test_toggle_enable_with_new_key_calls_notify(self, app, client, db_session):
        """Toggle enable (new key provisioned) should call _notify_mcp_reload."""
        from cms.auth import SETTING_MCP_SERVICE_KEY_HASH, get_setting

        # Ensure no existing key so toggle provisions a new one
        existing = await get_setting(db_session, SETTING_MCP_SERVICE_KEY_HASH)
        if existing:
            from cms.auth import set_setting
            await set_setting(db_session, SETTING_MCP_SERVICE_KEY_HASH, "")

        with patch("cms.ui._notify_mcp_reload", new_callable=AsyncMock) as mock_notify:
            resp = await client.post("/api/mcp/toggle", json={"enabled": True})
            assert resp.status_code == 200
            assert resp.json()["enabled"] is True
            assert "service_key" in resp.json()
            mock_notify.assert_called_once()

    async def test_toggle_disable_calls_notify(self, app, client, db_session):
        """Toggle disable (revoke key) should call _notify_mcp_reload."""
        from cms.auth import SETTING_MCP_ENABLED, set_setting
        await set_setting(db_session, SETTING_MCP_ENABLED, "true")

        with patch("cms.ui._notify_mcp_reload", new_callable=AsyncMock) as mock_notify:
            resp = await client.post("/api/mcp/toggle", json={"enabled": False})
            assert resp.status_code == 200
            assert resp.json()["enabled"] is False
            mock_notify.assert_called_once()

    async def test_toggle_enable_existing_key_does_not_call_notify(self, app, client, db_session):
        """Toggle enable when key already exists should NOT call _notify_mcp_reload."""
        from cms.auth import SETTING_MCP_SERVICE_KEY_HASH, provision_service_key, set_setting
        from cms.auth import get_settings

        settings = app.dependency_overrides[get_settings]()
        # Provision a key first so the toggle doesn't generate a new one
        await provision_service_key(db_session, settings.service_key_path)

        with patch("cms.ui._notify_mcp_reload", new_callable=AsyncMock) as mock_notify:
            resp = await client.post("/api/mcp/toggle", json={"enabled": True})
            assert resp.status_code == 200
            assert resp.json()["enabled"] is True
            # No new key was provisioned, so notify should NOT be called
            mock_notify.assert_not_called()
