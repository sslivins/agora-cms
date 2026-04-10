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
    reset_password: bool = False

    # Storage
    asset_storage_path: Path = Path("/opt/agora-cms/assets")
    storage_backend: str = "local"  # "local" or "azure"

    # Azure Blob Storage (only used when storage_backend == "azure")
    azure_storage_connection_string: str | None = None
    azure_storage_account_name: str | None = None
    azure_storage_account_key: str | None = None
    azure_sas_expiry_hours: int = 1

    # Asset downloads
    asset_base_url: str | None = None  # override base URL for device asset downloads

    # Device defaults
    default_device_storage_mb: int = 500  # assumed device flash budget for assets
    api_key_rotation_hours: int = 24  # rotate device API keys every N hours
    pending_device_ttl_hours: int = 24  # auto-purge pending devices not seen for N hours
