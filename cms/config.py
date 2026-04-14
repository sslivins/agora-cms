"""Agora CMS configuration."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    model_config = {"env_prefix": "AGORA_CMS_"}

    # Database
    database_url: str = "postgresql+asyncpg://agora:agora@localhost:5432/agora_cms"

    # Auth
    secret_key: str = Field(default="change-me-in-production")
    admin_username: str = "admin"
    admin_password: str = "agora"
    admin_email: str = "admin@localhost"
    reset_password: bool = False

    # Storage
    asset_storage_path: Path = Path("/opt/agora-cms/assets")
    storage_backend: str = "local"  # "local" or "azure"

    # Azure Blob Storage (only used when storage_backend == "azure")
    azure_storage_connection_string: str | None = None
    azure_storage_account_name: str | None = None
    azure_storage_account_key: str | None = None
    azure_sas_expiry_hours: int = 1

    # MCP Server
    mcp_server_url: str = "http://mcp:8000"  # Docker default; override for Azure

    # Asset downloads
    asset_base_url: str | None = None  # override base URL for device asset downloads

    # Device defaults
    default_device_storage_mb: int = 500  # assumed device flash budget for assets
    api_key_rotation_hours: int = 24  # rotate device API keys every N hours
    pending_device_ttl_hours: int = 24  # auto-purge pending devices not seen for N hours

    # MCP service key file (shared volume between CMS and MCP containers)
    service_key_path: str = "/shared/mcp-service.key"

    # SMTP is configured via the web UI settings page (stored in DB)
    base_url: str | None = None  # public URL for login links in emails
