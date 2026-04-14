"""Shared configuration — base settings used by both CMS and worker."""

from pathlib import Path

from pydantic_settings import BaseSettings


class SharedSettings(BaseSettings):
    model_config = {"env_prefix": "AGORA_CMS_"}

    # Database
    database_url: str = "postgresql+asyncpg://agora:agora@localhost:5432/agora_cms"

    # Storage
    asset_storage_path: Path = Path("/opt/agora-cms/assets")
    storage_backend: str = "local"  # "local" or "azure"

    # Azure Blob Storage (only used when storage_backend == "azure")
    azure_storage_connection_string: str | None = None
    azure_storage_account_name: str | None = None
    azure_storage_account_key: str | None = None
    azure_sas_expiry_hours: int = 1
