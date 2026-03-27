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

    # Storage
    asset_storage_path: Path = Path("/opt/agora-cms/assets")

    # Device defaults
    default_device_storage_mb: int = 500  # assumed device flash budget for assets
