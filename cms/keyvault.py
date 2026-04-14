"""Azure Key Vault helpers for MCP service key exchange.

In Azure Container Apps deployments, CMS and MCP containers cannot share
a filesystem volume.  Instead, CMS writes the service key to Key Vault
and MCP reads it using managed-identity credentials.

All Azure SDK imports are lazy so the module can be imported (and tested)
without the SDK installed.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

MCP_SECRET_NAME = "mcp-service-key"


def write_key_to_keyvault(vault_uri: str, raw_key: str) -> None:
    """Write (or overwrite) the MCP service key in Azure Key Vault."""
    from azure.identity import DefaultAzureCredential
    from azure.keyvault.secrets import SecretClient

    credential = DefaultAzureCredential()
    client = SecretClient(vault_url=vault_uri, credential=credential)
    client.set_secret(MCP_SECRET_NAME, raw_key)
    logger.info("MCP service key written to Key Vault (%s)", vault_uri)


def delete_key_from_keyvault(vault_uri: str) -> None:
    """Best-effort delete of the MCP service key from Azure Key Vault."""
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_uri, credential=credential)
        client.begin_delete_secret(MCP_SECRET_NAME)
        logger.info("MCP service key deleted from Key Vault (%s)", vault_uri)
    except Exception as exc:
        logger.warning("Failed to delete service key from Key Vault: %s", exc)


def read_key_from_keyvault(vault_uri: str) -> str:
    """Read the MCP service key from Azure Key Vault.

    Returns the key value, or empty string on failure.
    """
    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient

        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=vault_uri, credential=credential)
        secret = client.get_secret(MCP_SECRET_NAME)
        return (secret.value or "").strip()
    except Exception as exc:
        logger.warning("Failed to read service key from Key Vault: %s", exc)
        return ""
