"""Storage abstraction layer.

Provides a unified interface for file storage operations, supporting
both local filesystem (Docker volume) and Azure Blob Storage backends.

Local backend:
  Files live on disk at ``asset_storage_path``.  Downloads are served
  via FastAPI ``FileResponse``.  No cloud sync needed.

Azure backend:
  The local filesystem (an Azure Files mount) is the working copy for
  FFmpeg.  After writes, files are synced to Azure Blob Storage for
  durable, high-throughput device downloads via SAS URLs.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("agora.cms.storage")


# ── Module-level singleton ──────────────────────────────────────

_backend: StorageBackend | None = None


def init_storage(backend: StorageBackend) -> None:
    """Initialize the global storage backend (called once at startup)."""
    global _backend
    _backend = backend
    logger.info("Storage backend initialized: %s", type(backend).__name__)


def get_storage() -> StorageBackend:
    """Return the active storage backend."""
    if _backend is None:
        raise RuntimeError("Storage backend not initialized — call init_storage() first")
    return _backend


# ── Abstract base class ─────────────────────────────────────────

class StorageBackend(ABC):
    """Abstract interface for asset storage operations."""

    @abstractmethod
    def get_base_path(self) -> Path:
        """Return the local filesystem base path for asset I/O.

        FFmpeg, checksum computation, and probing all operate on this path.
        For local: Docker volume path.  For Azure: Azure Files mount.
        """

    @abstractmethod
    async def on_file_stored(self, relative_path: str) -> None:
        """Hook called after a file is written to the local filesystem.

        Azure backend copies the file to Blob Storage for permanent storage.
        Local backend is a no-op.
        """

    @abstractmethod
    async def on_file_deleted(self, relative_path: str) -> None:
        """Hook called when a file is deleted from the local filesystem.

        Azure backend also deletes from Blob Storage.
        Local backend is a no-op.
        """

    @abstractmethod
    async def get_download_response(
        self, relative_path: str, filename: str, media_type: str = "application/octet-stream",
    ):
        """Return an HTTP response to serve a file to a client.

        Local: ``FileResponse`` from disk.
        Azure: ``RedirectResponse`` to a SAS URL.
        """

    @abstractmethod
    async def get_device_download_url(
        self, relative_path: str, fallback_api_url: str,
    ) -> str:
        """Return a download URL for a device.

        Local: returns ``fallback_api_url`` (the CMS REST endpoint).
        Azure: returns a time-limited SAS URL for Blob Storage.
        """


# ── Local filesystem backend ────────────────────────────────────

class LocalStorageBackend(StorageBackend):
    """Stores assets on the local filesystem (Docker volume).

    All cloud sync hooks are no-ops.  Device downloads go through the
    CMS REST API (FileResponse).
    """

    def __init__(self, base_path: Path) -> None:
        self._base_path = base_path

    def get_base_path(self) -> Path:
        return self._base_path

    async def on_file_stored(self, relative_path: str) -> None:
        pass  # no cloud sync

    async def on_file_deleted(self, relative_path: str) -> None:
        pass  # no cloud sync

    async def get_download_response(
        self, relative_path: str, filename: str, media_type: str = "application/octet-stream",
    ):
        from fastapi.responses import FileResponse
        file_path = self._base_path / relative_path
        return FileResponse(path=file_path, filename=filename, media_type=media_type)

    async def get_device_download_url(
        self, relative_path: str, fallback_api_url: str,
    ) -> str:
        return fallback_api_url


# ── Azure Blob Storage backend ──────────────────────────────────

class AzureStorageBackend(StorageBackend):
    """Syncs assets to Azure Blob Storage; serves devices via SAS URLs.

    The local filesystem (Azure Files mount) is the FFmpeg workspace.
    After uploads/transcodes, files are copied to Blob for permanent
    storage and high-throughput device downloads.

    Blob layout mirrors the local filesystem structure:
      - Container ``originals``: source assets + pre-conversion originals
      - Container ``variants``:  transcoded variant files
    """

    def __init__(
        self,
        base_path: Path,
        connection_string: str,
        account_name: str | None = None,
        account_key: str | None = None,
        sas_expiry_hours: int = 1,
    ) -> None:
        from azure.storage.blob.aio import BlobServiceClient

        self._base_path = base_path
        self._connection_string = connection_string
        self._sas_expiry_hours = sas_expiry_hours

        self._service_client = BlobServiceClient.from_connection_string(connection_string)

        # Parse account name and key from connection string if not provided
        if account_name is None or account_key is None:
            parts = dict(
                part.split("=", 1) for part in connection_string.split(";") if "=" in part
            )
            self._account_name = account_name or parts.get("AccountName", "")
            self._account_key = account_key or parts.get("AccountKey", "")
        else:
            self._account_name = account_name
            self._account_key = account_key

    def get_base_path(self) -> Path:
        return self._base_path

    def _blob_location(self, relative_path: str) -> tuple[str, str]:
        """Map a local relative path to (container_name, blob_name).

        - ``variants/abc.mp4``       → (``variants``, ``abc.mp4``)
        - ``my_video.mp4``           → (``originals``, ``my_video.mp4``)
        - ``originals/photo.heic``   → (``originals``, ``originals/photo.heic``)
        """
        parts = Path(relative_path).parts
        if len(parts) > 1 and parts[0] == "variants":
            return "variants", "/".join(parts[1:])
        return "originals", relative_path

    async def on_file_stored(self, relative_path: str) -> None:
        """Upload the local file to Azure Blob Storage."""
        local_path = self._base_path / relative_path
        if not local_path.is_file():
            logger.warning("on_file_stored: local file not found: %s", local_path)
            return

        container, blob_name = self._blob_location(relative_path)
        try:
            container_client = self._service_client.get_container_client(container)
            blob_client = container_client.get_blob_client(blob_name)

            with open(local_path, "rb") as f:
                await blob_client.upload_blob(f, overwrite=True)

            logger.info("Synced to blob: %s/%s (%d bytes)", container, blob_name, local_path.stat().st_size)
        except Exception:
            logger.exception("Failed to sync %s to blob storage", relative_path)

    async def on_file_deleted(self, relative_path: str) -> None:
        """Delete the blob from Azure Blob Storage."""
        container, blob_name = self._blob_location(relative_path)
        try:
            container_client = self._service_client.get_container_client(container)
            blob_client = container_client.get_blob_client(blob_name)
            await blob_client.delete_blob(delete_snapshots="include")
            logger.info("Deleted blob: %s/%s", container, blob_name)
        except Exception:
            logger.exception("Failed to delete blob %s/%s", container, blob_name)

    def _generate_sas_url(self, container: str, blob_name: str) -> str:
        """Generate a time-limited SAS URL for a blob."""
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        sas_token = generate_blob_sas(
            account_name=self._account_name,
            account_key=self._account_key,
            container_name=container,
            blob_name=blob_name,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=self._sas_expiry_hours),
        )
        return (
            f"https://{self._account_name}.blob.core.windows.net"
            f"/{container}/{blob_name}?{sas_token}"
        )

    async def get_download_response(
        self, relative_path: str, filename: str, media_type: str = "application/octet-stream",
    ):
        """Redirect the client to a SAS URL for the blob."""
        from fastapi.responses import RedirectResponse
        container, blob_name = self._blob_location(relative_path)
        sas_url = self._generate_sas_url(container, blob_name)
        return RedirectResponse(url=sas_url, status_code=302)

    async def get_device_download_url(
        self, relative_path: str, fallback_api_url: str,
    ) -> str:
        """Return a SAS URL for device downloads (bypasses CMS proxy)."""
        container, blob_name = self._blob_location(relative_path)
        return self._generate_sas_url(container, blob_name)

    async def close(self) -> None:
        """Close the async blob service client."""
        await self._service_client.close()
