"""Unit tests for cms.keyvault helpers."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from cms.keyvault import (
    MCP_SECRET_NAME,
    delete_key_from_keyvault,
    read_key_from_keyvault,
    write_key_to_keyvault,
)

VAULT_URI = "https://test-vault.vault.azure.net/"


# ── write_key_to_keyvault ──


class TestWriteKey:
    @patch("azure.keyvault.secrets.SecretClient")
    @patch("azure.identity.DefaultAzureCredential")
    def test_writes_secret(self, mock_cred_cls, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        write_key_to_keyvault(VAULT_URI, "test_key_123")

        mock_cred_cls.assert_called_once()
        mock_client_cls.assert_called_once_with(
            vault_url=VAULT_URI, credential=mock_cred_cls.return_value
        )
        mock_client.set_secret.assert_called_once_with(MCP_SECRET_NAME, "test_key_123")

    @patch("azure.keyvault.secrets.SecretClient")
    @patch("azure.identity.DefaultAzureCredential")
    def test_raises_on_failure(self, mock_cred_cls, mock_client_cls):
        mock_client_cls.return_value.set_secret.side_effect = Exception("denied")
        with pytest.raises(Exception, match="denied"):
            write_key_to_keyvault(VAULT_URI, "key")


# ── delete_key_from_keyvault ──


class TestDeleteKey:
    @patch("azure.keyvault.secrets.SecretClient")
    @patch("azure.identity.DefaultAzureCredential")
    def test_deletes_secret(self, mock_cred_cls, mock_client_cls):
        mock_client = MagicMock()
        mock_client_cls.return_value = mock_client

        delete_key_from_keyvault(VAULT_URI)

        mock_client.begin_delete_secret.assert_called_once_with(MCP_SECRET_NAME)

    @patch("azure.keyvault.secrets.SecretClient")
    @patch("azure.identity.DefaultAzureCredential")
    def test_swallows_exception(self, mock_cred_cls, mock_client_cls):
        """Delete is best-effort — exceptions are logged but not raised."""
        mock_client_cls.return_value.begin_delete_secret.side_effect = Exception("boom")
        # Should NOT raise
        delete_key_from_keyvault(VAULT_URI)


# ── read_key_from_keyvault ──


class TestReadKey:
    @patch("azure.keyvault.secrets.SecretClient")
    @patch("azure.identity.DefaultAzureCredential")
    def test_reads_secret(self, mock_cred_cls, mock_client_cls):
        mock_secret = MagicMock()
        mock_secret.value = "agora_svc_abc123"
        mock_client_cls.return_value.get_secret.return_value = mock_secret

        result = read_key_from_keyvault(VAULT_URI)
        assert result == "agora_svc_abc123"

    @patch("azure.keyvault.secrets.SecretClient")
    @patch("azure.identity.DefaultAzureCredential")
    def test_strips_whitespace(self, mock_cred_cls, mock_client_cls):
        mock_secret = MagicMock()
        mock_secret.value = "  key_with_spaces  \n"
        mock_client_cls.return_value.get_secret.return_value = mock_secret

        assert read_key_from_keyvault(VAULT_URI) == "key_with_spaces"

    @patch("azure.keyvault.secrets.SecretClient")
    @patch("azure.identity.DefaultAzureCredential")
    def test_returns_empty_on_none_value(self, mock_cred_cls, mock_client_cls):
        mock_secret = MagicMock()
        mock_secret.value = None
        mock_client_cls.return_value.get_secret.return_value = mock_secret

        assert read_key_from_keyvault(VAULT_URI) == ""

    @patch("azure.keyvault.secrets.SecretClient")
    @patch("azure.identity.DefaultAzureCredential")
    def test_returns_empty_on_exception(self, mock_cred_cls, mock_client_cls):
        mock_client_cls.return_value.get_secret.side_effect = Exception("not found")
        assert read_key_from_keyvault(VAULT_URI) == ""
