"""Integration tests for Azure Key Vault service key exchange.

Tests cover:
- CMS provision/revoke with keyvault_uri param
- CMS UI endpoints (toggle, regenerate) passing keyvault_uri
- MCP server 3-tier key loading priority (env var > Key Vault > file)
- End-to-end key exchange (CMS writes → MCP reads same key)
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


# ── CMS-side: provision / revoke with keyvault ──


class TestProvisionWithKeyvault:
    @pytest.mark.asyncio
    async def test_provision_writes_to_keyvault(self, db_session):
        with patch("cms.keyvault.write_key_to_keyvault") as mock_write:
            from cms.auth import provision_service_key

            raw_key, prefix = await provision_service_key(
                db_session, "/tmp/test.key", keyvault_uri=VAULT_URI
            )
            mock_write.assert_called_once_with(VAULT_URI, raw_key)
            assert raw_key.startswith("agora_svc_")
            assert prefix.endswith("...")

    @pytest.mark.asyncio
    async def test_provision_skips_keyvault_when_not_configured(self, db_session):
        with patch("cms.keyvault.write_key_to_keyvault") as mock_write:
            from cms.auth import provision_service_key

            await provision_service_key(db_session, "/tmp/test.key")
            mock_write.assert_not_called()

    @pytest.mark.asyncio
    async def test_provision_keyvault_failure_propagates(self, db_session):
        with patch(
            "cms.keyvault.write_key_to_keyvault",
            side_effect=Exception("Key Vault write failed"),
        ):
            from cms.auth import provision_service_key

            with pytest.raises(Exception, match="Key Vault write failed"):
                await provision_service_key(
                    db_session, "/tmp/test.key", keyvault_uri=VAULT_URI
                )


class TestRevokeWithKeyvault:
    @pytest.mark.asyncio
    async def test_revoke_deletes_from_keyvault(self, db_session):
        with patch("cms.keyvault.delete_key_from_keyvault") as mock_delete:
            from cms.auth import revoke_service_key

            await revoke_service_key(
                db_session, "/tmp/test.key", keyvault_uri=VAULT_URI
            )
            mock_delete.assert_called_once_with(VAULT_URI)

    @pytest.mark.asyncio
    async def test_revoke_skips_keyvault_when_not_configured(self, db_session):
        with patch("cms.keyvault.delete_key_from_keyvault") as mock_delete:
            from cms.auth import revoke_service_key

            await revoke_service_key(db_session, "/tmp/test.key")
            mock_delete.assert_not_called()


# ── CMS UI endpoints with keyvault ──


class TestToggleEndpointWithKeyvault:
    @pytest.mark.asyncio
    async def test_toggle_enable_provisions_to_keyvault(self, app, client):
        from cms.auth import get_settings

        # Get the existing settings from the test fixture and set keyvault URI
        orig_settings = app.dependency_overrides[get_settings]()
        orig_uri = orig_settings.azure_keyvault_uri
        try:
            orig_settings.azure_keyvault_uri = VAULT_URI
            with patch("cms.keyvault.write_key_to_keyvault") as mock_write:
                resp = await client.post("/api/mcp/toggle", json={"enabled": True})
                assert resp.status_code == 200
                data = resp.json()
                assert data["enabled"] is True
                assert "service_key" in data
                mock_write.assert_called_once()
                args = mock_write.call_args[0]
                assert args[0] == VAULT_URI
                assert args[1] == data["service_key"]
        finally:
            orig_settings.azure_keyvault_uri = orig_uri


class TestRegenerateEndpointWithKeyvault:
    @pytest.mark.asyncio
    async def test_regenerate_writes_to_keyvault(self, app, client):
        from cms.auth import get_settings

        orig_settings = app.dependency_overrides[get_settings]()
        orig_uri = orig_settings.azure_keyvault_uri
        try:
            orig_settings.azure_keyvault_uri = VAULT_URI
            with patch("cms.keyvault.write_key_to_keyvault") as mock_write:
                resp = await client.post("/api/mcp/service-key/regenerate")
                assert resp.status_code == 200
                data = resp.json()
                assert data["regenerated"] is True
                mock_write.assert_called_once()
                assert mock_write.call_args[0][0] == VAULT_URI
        finally:
            orig_settings.azure_keyvault_uri = orig_uri


# ── MCP-side: 3-tier key loading ──


@pytest.fixture
def mcp_server_module(tmp_path):
    """Import mcp/server.py as a standalone module with mocked MCP SDK."""
    saved_modules = {}
    # Create mock modules for mcp SDK
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
        # Restore saved modules
        for mod_name, original in saved_modules.items():
            if original is None:
                sys.modules.pop(mod_name, None)
            else:
                sys.modules[mod_name] = original


class TestMcpKeyLoadingPriority:
    def test_env_var_highest_priority(self, mcp_server_module, tmp_path):
        """SERVICE_KEY env var takes highest priority."""
        mod = mcp_server_module
        key_file = tmp_path / "svc.key"
        key_file.write_text("file_key_value")

        env = {
            "SERVICE_KEY": "env_key_value",
            "AZURE_KEYVAULT_URI": "https://vault.vault.azure.net/",
            "SERVICE_KEY_PATH": str(key_file),
        }
        with patch.dict(os.environ, env):
            mod.AZURE_KEYVAULT_URI = "https://vault.vault.azure.net/"
            mod.SERVICE_KEY_PATH = str(key_file)
            key, source = mod._load_service_key_sync()
            assert key == "env_key_value"
            assert source == "SERVICE_KEY env var"

    def test_keyvault_second_priority(self, mcp_server_module, tmp_path):
        """Key Vault is checked when no SERVICE_KEY env var."""
        mod = mcp_server_module
        key_file = tmp_path / "svc.key"
        key_file.write_text("file_key_value")

        env = {"SERVICE_KEY_PATH": str(key_file)}
        with (
            patch.dict(os.environ, env, clear=False),
            patch.object(mod, "_read_key_from_keyvault", return_value="kv_key_value"),
        ):
            # Remove SERVICE_KEY from env
            os.environ.pop("SERVICE_KEY", None)
            mod.AZURE_KEYVAULT_URI = "https://vault.vault.azure.net/"
            mod.SERVICE_KEY_PATH = str(key_file)
            key, source = mod._load_service_key_sync()
            assert key == "kv_key_value"
            assert "Key Vault" in source

    def test_file_fallback(self, mcp_server_module, tmp_path):
        """File is used when no env var and no Key Vault."""
        mod = mcp_server_module
        key_file = tmp_path / "svc.key"
        key_file.write_text("file_key_value")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SERVICE_KEY", None)
            mod.AZURE_KEYVAULT_URI = ""
            mod.SERVICE_KEY_PATH = str(key_file)
            key, source = mod._load_service_key_sync()
            assert key == "file_key_value"
            assert source == str(key_file)

    def test_empty_when_nothing_found(self, mcp_server_module, tmp_path):
        """Returns empty tuple when no key source available."""
        mod = mcp_server_module
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SERVICE_KEY", None)
            mod.AZURE_KEYVAULT_URI = ""
            mod.SERVICE_KEY_PATH = str(tmp_path / "nonexistent.key")
            key, source = mod._load_service_key_sync()
            assert key == ""
            assert source == ""

    def test_keyvault_empty_falls_through_to_file(self, mcp_server_module, tmp_path):
        """When Key Vault returns empty, falls through to file."""
        mod = mcp_server_module
        key_file = tmp_path / "svc.key"
        key_file.write_text("file_key_value")

        with (
            patch.dict(os.environ, {}, clear=False),
            patch.object(mod, "_read_key_from_keyvault", return_value=""),
        ):
            os.environ.pop("SERVICE_KEY", None)
            mod.AZURE_KEYVAULT_URI = "https://vault.vault.azure.net/"
            mod.SERVICE_KEY_PATH = str(key_file)
            key, source = mod._load_service_key_sync()
            assert key == "file_key_value"
            assert source == str(key_file)

    def test_env_var_whitespace_only_skipped(self, mcp_server_module, tmp_path):
        """Whitespace-only SERVICE_KEY is treated as empty."""
        mod = mcp_server_module
        key_file = tmp_path / "svc.key"
        key_file.write_text("file_key_value")

        with patch.dict(os.environ, {"SERVICE_KEY": "   "}, clear=False):
            mod.AZURE_KEYVAULT_URI = ""
            mod.SERVICE_KEY_PATH = str(key_file)
            key, source = mod._load_service_key_sync()
            assert key == "file_key_value"


class TestMcpReadKeyFromKeyvault:
    def test_reads_from_keyvault(self, mcp_server_module):
        mod = mcp_server_module
        with (
            patch("azure.identity.DefaultAzureCredential") as mock_cred,
            patch("azure.keyvault.secrets.SecretClient") as mock_client_cls,
        ):
            mock_secret = MagicMock()
            mock_secret.value = "kv_service_key"
            mock_client_cls.return_value.get_secret.return_value = mock_secret

            result = mod._read_key_from_keyvault(VAULT_URI)
            assert result == "kv_service_key"

    def test_returns_empty_on_failure(self, mcp_server_module):
        mod = mcp_server_module
        with (
            patch("azure.identity.DefaultAzureCredential"),
            patch(
                "azure.keyvault.secrets.SecretClient",
                side_effect=Exception("connection failed"),
            ),
        ):
            result = mod._read_key_from_keyvault(VAULT_URI)
            assert result == ""


# ── End-to-end key exchange ──


class TestEndToEndKeyExchange:
    @pytest.mark.asyncio
    async def test_provision_then_mcp_reads_same_key(self, db_session):
        """CMS provisions a key to KV, MCP reads it back — keys must match."""
        kv_store: dict[str, str] = {}

        def fake_write(uri, key):
            kv_store["mcp-service-key"] = key

        def fake_read(uri):
            return kv_store.get("mcp-service-key", "")

        with (
            patch("cms.keyvault.write_key_to_keyvault", side_effect=fake_write),
            patch(
                "cms.keyvault.read_key_from_keyvault", side_effect=fake_read
            ),
        ):
            from cms.auth import provision_service_key

            raw_key, _ = await provision_service_key(
                db_session, "/tmp/test.key", keyvault_uri=VAULT_URI
            )
            read_back = fake_read(VAULT_URI)
            assert read_back == raw_key

    @pytest.mark.asyncio
    async def test_regenerate_updates_keyvault_key(self, db_session):
        """Regenerating the key overwrites the KV entry."""
        kv_store: dict[str, str] = {}

        def fake_write(uri, key):
            kv_store["mcp-service-key"] = key

        with patch("cms.keyvault.write_key_to_keyvault", side_effect=fake_write):
            from cms.auth import provision_service_key

            key1, _ = await provision_service_key(
                db_session, "/tmp/test.key", keyvault_uri=VAULT_URI
            )
            key2, _ = await provision_service_key(
                db_session, "/tmp/test.key", keyvault_uri=VAULT_URI
            )
            assert key1 != key2
            assert kv_store["mcp-service-key"] == key2
