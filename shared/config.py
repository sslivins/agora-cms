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

    # ── Imager (browser-driven Pi image provisioning, Option E) ────
    # NOTE (PR 7): the catalog URL was previously a deploy-time env var
    # (``base_image_catalog_url``) but is now stored as a runtime setting
    # in the ``cms_settings`` table (key ``imager.catalog_url``) and
    # edited via ``PUT /api/imager/settings``.  See
    # :mod:`cms.services.imager_settings`.
    # Hostname allowlist for upstream catalog + image fetches.  The
    # worker refuses fetches whose URL host is not in this list.
    # Default covers GitHub Releases (the only host upstream uses).
    base_image_allowed_hosts: str = "github.com,objects.githubusercontent.com"
    # Tenant blob containers for the imager pipeline.
    base_image_cache_container: str = "base-images"
    provisioned_container: str = "provisioned"
    # Output retention before Azure lifecycle policy auto-deletes
    # blobs.  The CMS row is preserved for audit; only the bytes go.
    provisioned_retention_hours: int = 24
    # SAS TTL for download URLs handed back to the admin browser.
    imager_sas_ttl_hours: int = 2
    # Optional dedicated scratch directory for the imager handlers.
    # Defaults to a subdirectory under ``asset_storage_path`` so the
    # worker doesn't need extra mounts in the simple case; production
    # deployments should override with a path on a dedicated volume.
    imager_scratch_path: Path | None = None
    # Minimum free bytes the worker requires on the scratch volume
    # before starting an imager job.  ~10 GiB headroom over the
    # ~6 GiB worst-case peak (decompress + recompress in parallel).
    imager_min_free_bytes: int = 10 * 1024 * 1024 * 1024
