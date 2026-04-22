"""Device-log blob storage helper (Stage 3b of #345).

Dedicated storage for device log bundles (tar.gz archives produced by
``request_logs``).  Separate from :mod:`shared.services.storage`, which
is coupled to the asset pipeline's Azure Files mount + FFmpeg workspace
and has no notion of on-the-fly streaming uploads or short-lived
download URLs.

Backends:

* **Local** — files land under ``<asset_storage_path>/device-logs/``.
  Reads stream off disk, downloads are :class:`FileResponse`.
* **Azure** — files land in the ``device-logs`` Blob container.  Reads
  stream from Blob; downloads redirect to a 1-hour SAS URL so the
  browser talks directly to storage and the CMS never buffers the tar.

See ``docs/multi-replica-architecture.md`` §Stage 3 for the design.
"""

from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Union

logger = logging.getLogger("agora.cms.log_blob")


# Blob container / on-disk subdirectory name.
LOG_CONTAINER = "device-logs"

# Read buffer for on-disk streaming reads.
_CHUNK_SIZE = 1 << 20  # 1 MiB

# SAS URL lifetime for downloads.
_SAS_EXPIRY_HOURS = 1


BytesLike = Union[bytes, bytearray, memoryview]
DataSource = Union[BytesLike, AsyncIterator[bytes]]


# ── Backend ABC ─────────────────────────────────────────────────────

class LogBlobBackend(ABC):
    @abstractmethod
    async def init(self) -> None:
        """Prepare the backend — create container / mkdir base path."""

    @abstractmethod
    async def write(self, relative_path: str, data: DataSource) -> int:
        """Write a blob.  Accepts either ``bytes`` or an ``AsyncIterator``
        of chunks.  Returns the total number of bytes written."""

    @abstractmethod
    async def read(self, relative_path: str) -> AsyncIterator[bytes]:
        """Stream a blob's bytes."""

    @abstractmethod
    async def delete(self, relative_path: str) -> bool:
        """Delete a blob.  Returns ``True`` iff it existed."""

    @abstractmethod
    async def get_download_response(self, relative_path: str, filename: str):
        """Return a FastAPI Response that serves the blob.

        Local: :class:`FileResponse`.  Azure: :class:`RedirectResponse`
        to a short-lived SAS URL.
        """


# ── Local filesystem backend ────────────────────────────────────────

class LocalLogBlobBackend(LogBlobBackend):
    def __init__(self, base_path: Path) -> None:
        # Files land under <asset_storage_path>/device-logs/ so local
        # test rigs and single-box deployments have just one folder to
        # mount + back up.
        self._base_path = base_path / LOG_CONTAINER

    async def init(self) -> None:
        self._base_path.mkdir(parents=True, exist_ok=True)
        logger.info("Log blob backend: Local at %s", self._base_path)

    def _resolve(self, relative_path: str) -> Path:
        # Guard against path traversal — the blob path is built from
        # trusted ids in production, but enforcing this here keeps the
        # helper safe to call from anywhere.
        clean = os.path.normpath(relative_path).replace("\\", "/")
        if clean.startswith("..") or clean.startswith("/"):
            raise ValueError(f"invalid log blob path: {relative_path!r}")
        return self._base_path / clean

    async def write(self, relative_path: str, data: DataSource) -> int:
        path = self._resolve(relative_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        written = 0
        if isinstance(data, (bytes, bytearray, memoryview)):
            buf = bytes(data)
            path.write_bytes(buf)
            written = len(buf)
        else:
            # AsyncIterator[bytes]
            with path.open("wb") as fh:
                async for chunk in data:
                    if not chunk:
                        continue
                    fh.write(chunk)
                    written += len(chunk)
        return written

    async def read(self, relative_path: str) -> AsyncIterator[bytes]:
        path = self._resolve(relative_path)
        if not path.is_file():
            raise FileNotFoundError(relative_path)

        async def _iter() -> AsyncIterator[bytes]:
            with path.open("rb") as fh:
                while True:
                    chunk = fh.read(_CHUNK_SIZE)
                    if not chunk:
                        break
                    yield chunk

        return _iter()

    async def delete(self, relative_path: str) -> bool:
        path = self._resolve(relative_path)
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return False

    async def get_download_response(self, relative_path: str, filename: str):
        from fastapi.responses import FileResponse

        path = self._resolve(relative_path)
        return FileResponse(
            path=path, filename=filename, media_type="application/gzip",
        )


# ── Azure Blob Storage backend ──────────────────────────────────────

class AzureLogBlobBackend(LogBlobBackend):
    def __init__(
        self,
        connection_string: str,
        account_name: str | None = None,
        account_key: str | None = None,
    ) -> None:
        from azure.storage.blob.aio import BlobServiceClient

        self._connection_string = connection_string
        self._service_client = BlobServiceClient.from_connection_string(connection_string)

        if account_name is None or account_key is None:
            parts = dict(
                part.split("=", 1) for part in connection_string.split(";") if "=" in part
            )
            self._account_name = account_name or parts.get("AccountName", "")
            self._account_key = account_key or parts.get("AccountKey", "")
        else:
            self._account_name = account_name
            self._account_key = account_key

    async def init(self) -> None:
        container_client = self._service_client.get_container_client(LOG_CONTAINER)
        try:
            await container_client.create_container()
            logger.info("Log blob backend: created Azure container %s", LOG_CONTAINER)
        except Exception as exc:  # noqa: BLE001 — ResourceExistsError is fine
            # Common case: container already exists; log at debug.
            if type(exc).__name__ == "ResourceExistsError":
                logger.debug("Log blob container %s already exists", LOG_CONTAINER)
            else:
                logger.warning(
                    "Log blob init: container create returned %s; assuming it exists",
                    exc,
                )
        logger.info("Log blob backend: Azure container %s", LOG_CONTAINER)

    def _blob_client(self, relative_path: str):
        container = self._service_client.get_container_client(LOG_CONTAINER)
        return container.get_blob_client(relative_path)

    async def write(self, relative_path: str, data: DataSource) -> int:
        client = self._blob_client(relative_path)
        if isinstance(data, (bytes, bytearray, memoryview)):
            buf = bytes(data)
            await client.upload_blob(buf, overwrite=True)
            return len(buf)

        # Buffer the async iterator into memory before upload.  The
        # caller already enforces the 100 MB cap on the upload endpoint
        # so this is bounded.  The Azure async SDK can accept an
        # iterable of bytes; some versions do not support an async one
        # reliably, so a single upload keeps things simple + correct.
        chunks: list[bytes] = []
        total = 0
        async for chunk in data:
            if not chunk:
                continue
            chunks.append(chunk)
            total += len(chunk)
        payload = b"".join(chunks)
        await client.upload_blob(payload, overwrite=True)
        return total

    async def read(self, relative_path: str) -> AsyncIterator[bytes]:
        client = self._blob_client(relative_path)
        downloader = await client.download_blob()

        async def _iter() -> AsyncIterator[bytes]:
            async for chunk in downloader.chunks():
                yield chunk

        return _iter()

    async def delete(self, relative_path: str) -> bool:
        client = self._blob_client(relative_path)
        try:
            await client.delete_blob(delete_snapshots="include")
            return True
        except Exception as exc:  # noqa: BLE001
            if type(exc).__name__ == "ResourceNotFoundError":
                return False
            logger.exception("delete_log_blob failed for %s", relative_path)
            return False

    def _sas_url(self, relative_path: str) -> str:
        from azure.storage.blob import BlobSasPermissions, generate_blob_sas

        sas_token = generate_blob_sas(
            account_name=self._account_name,
            account_key=self._account_key,
            container_name=LOG_CONTAINER,
            blob_name=relative_path,
            permission=BlobSasPermissions(read=True),
            expiry=datetime.now(timezone.utc) + timedelta(hours=_SAS_EXPIRY_HOURS),
        )
        return (
            f"https://{self._account_name}.blob.core.windows.net"
            f"/{LOG_CONTAINER}/{relative_path}?{sas_token}"
        )

    async def get_download_response(self, relative_path: str, filename: str):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url=self._sas_url(relative_path), status_code=302)

    async def close(self) -> None:
        await self._service_client.close()


# ── Module-level singleton ──────────────────────────────────────────

_backend: LogBlobBackend | None = None


async def init_log_storage(settings) -> None:
    """Initialise the global log-blob backend.

    Called once from the app startup hook.  Picks Azure vs Local the
    same way the asset storage does (``settings.storage_backend``).
    """
    global _backend
    if settings.storage_backend == "azure":
        if not settings.azure_storage_connection_string:
            raise RuntimeError(
                "AGORA_CMS_AZURE_STORAGE_CONNECTION_STRING is required "
                "when storage_backend is 'azure'"
            )
        _backend = AzureLogBlobBackend(
            connection_string=settings.azure_storage_connection_string,
            account_name=settings.azure_storage_account_name,
            account_key=settings.azure_storage_account_key,
        )
    else:
        _backend = LocalLogBlobBackend(base_path=settings.asset_storage_path)
    await _backend.init()


def get_log_backend() -> LogBlobBackend:
    if _backend is None:
        raise RuntimeError(
            "Log blob backend not initialised — call init_log_storage() first"
        )
    return _backend


def set_log_backend(backend: LogBlobBackend | None) -> None:
    """Test helper — swap the backend without re-running init()."""
    global _backend
    _backend = backend


# ── Public async API ────────────────────────────────────────────────

async def write_log_blob(relative_path: str, data: DataSource) -> int:
    """Persist ``data`` under ``relative_path``.  Returns bytes written."""
    return await get_log_backend().write(relative_path, data)


async def read_log_blob(relative_path: str) -> AsyncIterator[bytes]:
    """Stream the bytes of a previously-written blob."""
    return await get_log_backend().read(relative_path)


async def delete_log_blob(relative_path: str) -> bool:
    """Delete a blob.  Returns ``True`` iff it existed."""
    return await get_log_backend().delete(relative_path)


async def get_log_download_response(relative_path: str, filename: str):
    """Return a FastAPI response that delivers the blob to a browser."""
    return await get_log_backend().get_download_response(relative_path, filename)
