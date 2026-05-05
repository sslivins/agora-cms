"""Worker handlers for browser-driven Pi image provisioning (PR 3).

Two job types from ``JobType``:

* ``IMAGE_IMPORT``  -- fetch upstream ``.img.xz`` + ``catalog.json``
  entry, verify SHA256, upload to tenant blob.  One per
  ``(variant, version)`` per tenant; idempotent.
* ``IMAGE_PROVISION`` -- decompress base, drop ``agora-fleet.env``
  onto FAT boot partition (``cms.services.imager.build_provisioned``),
  recompress, upload to ``provisioned/<id>/<output_name>``.  One per
  per-fleet build; on demand.

Both follow the existing dispatcher contract used by transcode +
capture handlers:

* ``async def handler(session_factory, settings, target_id) -> bool``
* Return ``True`` for success (dispatcher marks Job DONE, deletes msg).
* Return ``False`` for transient failure (dispatcher flips Job back
  to ``PENDING``, leaves queue msg visible -> retry after
  ``VISIBILITY_TIMEOUT``).
* Raise ``TerminalImagerError`` for deterministic failures that
  retrying cannot fix (SHA mismatch, allowlist violation, malformed
  blob path).  The dispatcher catches this and marks Job FAILED +
  deletes the queue msg so retries don't burn cycles.
* Raise any other exception for unexpected errors -- dispatcher
  treats these as transient (retry).

Subprocess work in ``cms.services.imager.build_provisioned`` is
synchronous and slow (~minutes).  We dispatch it via
``asyncio.to_thread`` so the dispatcher's heartbeat coroutine keeps
renewing the queue lease while the imager runs.

Secret hygiene
--------------
``ProvisionedImage.fleet_env_payload`` is the device API key bundle.
PR 4 wraps this in real encryption; in PR 3 we treat the bytes as
plaintext UTF-8 (the API endpoint in PR 4 will be the only writer
once it lands).  After terminal success **or** terminal failure we
clear the payload to ``None`` so the secret does not linger in the
DB longer than necessary.  For retryable failures we leave the
payload in place because the next attempt needs it.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select

from cms.services.imager import ImagerError, build_provisioned
from cms.services.imager_settings import get_catalog_url
from shared.models.imager import (
    BaseImage,
    BaseImageStatus,
    ProvisionedImage,
    ProvisionedImageStatus,
)
from shared.services.imager_catalog import (
    HTTP_TIMEOUT as _HTTP_TIMEOUT,
    MAX_REDIRECTS as _MAX_REDIRECTS,
    CatalogError,
    fetch_catalog as _fetch_catalog_shared,
    parse_allowed_hosts,
    validate_url as _validate_url_shared,
)
from shared.services.storage import get_storage

logger = logging.getLogger(__name__)


# ── Public exceptions ─────────────────────────────────────────────


class TerminalImagerError(Exception):
    """Deterministic imager failure -- do not retry.

    Raised by the handlers for conditions that no number of retries
    will fix:

    * Catalog or download URL host not in the allowlist.
    * SHA256 of downloaded base image does not match the catalog.
    * Tenant-blob base image SHA256 has drifted from the row's
      ``sha256`` (storage tampering).
    * ``BaseImage`` row in ``FAILED`` state when a provision job
      tries to consume it.
    * Malformed ``blob_path`` / ``output_name`` (regex deny).
    * Decoded ``fleet_env_payload`` not valid UTF-8.

    The dispatcher in ``worker/__main__.py`` catches this exception
    explicitly, marks the ``Job`` row ``FAILED``, and deletes the
    queue message so it does not redeliver.
    """


# ── Constants ─────────────────────────────────────────────────────


# Container-relative blob_path produced by the import handler.  The
# read side (provision handler) re-validates against the same regex
# to guard against a corrupted/tampered DB.  Two path segments
# (variant / version) followed by the literal ``base.img.xz``.
_BASE_BLOB_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/base\.img\.xz$")
# 64 hex chars -- defensive guard before any download.
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
# Bound the chunked download buffer.
_DOWNLOAD_CHUNK_BYTES = 1 << 20  # 1 MiB


# ── Internal helpers ──────────────────────────────────────────────


def _allowed_hosts(settings: Any) -> set[str]:
    return parse_allowed_hosts(getattr(settings, "base_image_allowed_hosts", ""))


def _validate_url(url: str, allowlist: set[str]) -> str:
    """Wrap the shared validator and surface failures as terminal."""
    try:
        return _validate_url_shared(url, allowlist)
    except CatalogError as e:
        raise TerminalImagerError(str(e)) from e


async def _free_bytes(path: Path) -> int:
    """Disk-usage probe; offloaded to a thread because ``statvfs``
    can block on networked filesystems (Azure Files mount in prod).
    """
    return await asyncio.to_thread(lambda: shutil.disk_usage(path).free)


def _scratch_dir(settings: Any, prefix: str, target_id: uuid.UUID) -> Path:
    """Per-attempt scratch directory.

    Includes a fresh ``uuid4`` suffix so a duplicate pickup of the
    same queue message (lease loss) lands in its own directory and
    cannot rmtree another worker's in-flight files.
    """
    root = Path(settings.imager_scratch_path)
    return root / f"{prefix}-{target_id}-{uuid.uuid4().hex[:8]}"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


async def _set_base_image_terminal(
    session_factory: Any, base_image_id: uuid.UUID, message: str
) -> None:
    """Mark a ``BaseImage`` row FAILED with ``message`` (truncated)."""
    async with session_factory() as db:
        row = (await db.execute(
            select(BaseImage).where(BaseImage.id == base_image_id)
        )).scalar_one_or_none()
        if row is None:
            return
        row.status = BaseImageStatus.FAILED.value
        row.error_message = message[:2000]
        await db.commit()


async def _set_provisioned_terminal(
    session_factory: Any, provisioned_id: uuid.UUID, message: str
) -> None:
    """Mark a ``ProvisionedImage`` row FAILED with ``message`` and
    clear the encrypted payload (the secret is no longer needed)."""
    async with session_factory() as db:
        row = (await db.execute(
            select(ProvisionedImage).where(ProvisionedImage.id == provisioned_id)
        )).scalar_one_or_none()
        if row is None:
            return
        row.status = ProvisionedImageStatus.FAILED.value
        row.error_message = message[:2000]
        # Secret hygiene: terminal failures clear the payload.  Retryable
        # failures leave it in place because the next attempt needs it.
        row.fleet_env_payload = None
        await db.commit()


async def _fetch_catalog(
    catalog_url: str, allowlist: set[str], client: httpx.AsyncClient
) -> dict[str, Any]:
    """Wrap the shared catalog fetcher; surface CatalogError as terminal."""
    try:
        return await _fetch_catalog_shared(catalog_url, allowlist, client)
    except CatalogError as e:
        raise TerminalImagerError(str(e)) from e


async def _download_with_sha(
    url: str,
    dest: Path,
    expected_sha256: str,
    expected_size_bytes: int | None,
    allowlist: set[str],
    client: httpx.AsyncClient,
) -> tuple[str, int]:
    """Stream ``url`` to ``dest`` while hashing.

    Validates each redirect Location against ``allowlist``; aborts
    early if the running byte count exceeds ``expected_size_bytes``
    (avoids filling the disk on a malicious catalog).  Returns
    ``(actual_sha256_hex, actual_bytes)``.

    Raises ``TerminalImagerError`` on hash mismatch / size mismatch /
    allowlist redirect violation.  Raises generic httpx errors on
    transient network problems (caught upstream + retried).
    """
    if not _SHA256_RE.match(expected_sha256):
        raise TerminalImagerError(
            f"expected_sha256 is not a 64-char hex string: {expected_sha256!r}"
        )

    # Manual redirect walk -- httpx's built-in follower won't let us
    # validate every hop's host against our allowlist.
    target_url = _validate_url(url, allowlist)
    hops = 0
    while True:
        async with client.stream("GET", target_url) as resp:
            if resp.is_redirect and hops < _MAX_REDIRECTS:
                loc = resp.headers.get("location")
                if not loc:
                    raise httpx.RemoteProtocolError("redirect without Location")
                target_url = _validate_url(loc, allowlist)
                hops += 1
                continue
            if resp.is_redirect:
                raise TerminalImagerError(
                    f"too many redirects ({_MAX_REDIRECTS}) downloading image"
                )
            resp.raise_for_status()

            hasher = hashlib.sha256()
            written = 0
            with dest.open("wb") as fh:
                async for chunk in resp.aiter_bytes(_DOWNLOAD_CHUNK_BYTES):
                    if not chunk:
                        continue
                    written += len(chunk)
                    if (
                        expected_size_bytes is not None
                        and written > expected_size_bytes
                    ):
                        raise TerminalImagerError(
                            f"download exceeded expected size "
                            f"({written} > {expected_size_bytes})"
                        )
                    hasher.update(chunk)
                    fh.write(chunk)
            actual_sha = hasher.hexdigest()
            if actual_sha.lower() != expected_sha256.lower():
                raise TerminalImagerError(
                    f"sha256 mismatch: expected {expected_sha256.lower()}, "
                    f"got {actual_sha.lower()}"
                )
            if (
                expected_size_bytes is not None
                and written != expected_size_bytes
            ):
                raise TerminalImagerError(
                    f"size mismatch: expected {expected_size_bytes}, got {written}"
                )
            return actual_sha, written


async def _hash_file(path: Path) -> str:
    """SHA256 a local file off the event loop."""
    def _do() -> str:
        h = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(_DOWNLOAD_CHUNK_BYTES), b""):
                h.update(chunk)
        return h.hexdigest()
    return await asyncio.to_thread(_do)


# ── Public handlers ───────────────────────────────────────────────


async def import_base_image_by_id(
    session_factory: Any, settings: Any, base_image_id: uuid.UUID
) -> bool:
    """Import one upstream base image into tenant blob.

    Returns True on success (or idempotent no-op when the row is
    already READY).  Returns False for retryable failures (caller
    will redeliver the queue message).  Raises ``TerminalImagerError``
    for deterministic failures.
    """
    storage = get_storage()
    allowlist = _allowed_hosts(settings)
    scratch: Path | None = None

    try:
        async with session_factory() as db:
            row = (await db.execute(
                select(BaseImage).where(BaseImage.id == base_image_id)
            )).scalar_one_or_none()
            if row is None:
                logger.info(
                    "BaseImage %s no longer exists -- skipping", base_image_id
                )
                return False
            if row.status == BaseImageStatus.READY.value:
                logger.info(
                    "BaseImage %s already READY -- idempotent skip", base_image_id
                )
                return True
            variant = row.variant
            version = row.version
            source_url = row.source_url
            expected_sha256 = row.expected_sha256
            # PR 7: catalog URL moved from env var to DB setting.
            # Read it in the same session so the fallback below has it.
            catalog_url = await get_catalog_url(db)

        # Per-attempt scratch directory.
        scratch = _scratch_dir(settings, "import", base_image_id)
        scratch.mkdir(parents=True, exist_ok=True)

        # Free-space pre-flight (retryable -- another job may free disk soon).
        free = await _free_bytes(scratch)
        if free < settings.imager_min_free_bytes:
            logger.warning(
                "BaseImage %s import: free space %d below threshold %d -- retry",
                base_image_id, free, settings.imager_min_free_bytes,
            )
            return False

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT) as client:
            # Resolve the catalog if the API didn't stamp source_url +
            # expected_sha256 at enqueue time.  PR 4 will populate both
            # at insert time, eliminating this fallback.
            if not source_url or not expected_sha256:
                if not catalog_url:
                    raise TerminalImagerError(
                        "imager catalog URL is not configured and row has "
                        "no stamped source_url/expected_sha256"
                    )
                catalog = await _fetch_catalog(catalog_url, allowlist, client)
                variants = catalog.get("variants") or {}
                entry = variants.get(variant)
                if not isinstance(entry, dict):
                    raise TerminalImagerError(
                        f"catalog has no entry for variant {variant!r}"
                    )
                if catalog.get("ref") and catalog["ref"] != version:
                    raise TerminalImagerError(
                        f"catalog ref {catalog.get('ref')!r} != row version {version!r}"
                    )
                source_url = entry.get("url")
                expected_sha256 = entry.get("sha256")
                expected_size = entry.get("size_bytes")
                if not source_url or not expected_sha256:
                    raise TerminalImagerError(
                        f"catalog entry for {variant!r} missing url/sha256"
                    )
            else:
                expected_size = None  # PR 4 may add expected_size_bytes col

            staged = scratch / "base.img.xz"
            actual_sha, actual_size = await _download_with_sha(
                source_url, staged, expected_sha256, expected_size,
                allowlist, client,
            )

        blob_path = f"{variant}/{version}/base.img.xz"
        if not _BASE_BLOB_PATH_RE.match(blob_path):
            # variant/version are admin-controlled in PR 4 but
            # Postgres-stored as Text -- defence in depth.
            raise TerminalImagerError(
                f"refusing malformed blob_path {blob_path!r} (variant/version "
                f"contain disallowed characters)"
            )

        await storage.upload_local_file(
            settings.base_image_cache_container,
            blob_path,
            staged,
            overwrite=True,
        )

        async with session_factory() as db:
            row = (await db.execute(
                select(BaseImage).where(BaseImage.id == base_image_id)
            )).scalar_one_or_none()
            if row is None:
                # Row vanished between start and end -- we still
                # uploaded a blob, but there's nothing to flip ready.
                logger.warning(
                    "BaseImage %s row vanished post-upload -- not flipping READY",
                    base_image_id,
                )
                return False
            row.sha256 = actual_sha.lower()
            row.size_bytes = actual_size
            row.blob_path = blob_path
            row.imported_at = _utcnow()
            row.status = BaseImageStatus.READY.value
            row.error_message = ""
            await db.commit()

        logger.info(
            "BaseImage %s imported: %s (%d bytes)",
            base_image_id, blob_path, actual_size,
        )
        return True
    except TerminalImagerError as e:
        logger.error("BaseImage %s import terminal failure: %s", base_image_id, e)
        await _set_base_image_terminal(session_factory, base_image_id, str(e))
        raise
    finally:
        if scratch is not None and scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)


async def provision_image_by_id(
    session_factory: Any, settings: Any, provisioned_id: uuid.UUID
) -> bool:
    """Build one per-fleet provisioned ``.img.xz``.

    See module docstring for the contract.  Returns True on success
    or idempotent no-op; False for retryable; raises
    ``TerminalImagerError`` for deterministic failures.
    """
    storage = get_storage()
    scratch: Path | None = None

    try:
        async with session_factory() as db:
            row = (await db.execute(
                select(ProvisionedImage).where(
                    ProvisionedImage.id == provisioned_id
                )
            )).scalar_one_or_none()
            if row is None:
                logger.info(
                    "ProvisionedImage %s no longer exists -- skipping",
                    provisioned_id,
                )
                return False
            if row.status == ProvisionedImageStatus.READY.value:
                logger.info(
                    "ProvisionedImage %s already READY -- idempotent skip",
                    provisioned_id,
                )
                return True
            base_image_id = row.base_image_id
            output_name = row.output_name
            payload = row.fleet_env_payload

            base = (await db.execute(
                select(BaseImage).where(BaseImage.id == base_image_id)
            )).scalar_one_or_none()
            if base is None:
                raise TerminalImagerError(
                    f"base image {base_image_id} not found for provision"
                )
            base_status = base.status
            base_blob_path = base.blob_path
            base_sha256 = base.sha256

        # Base must be READY before we can provision off it.  If it's
        # IMPORTING the base import is in flight -- be patient and let
        # the queue redeliver.  FAILED is terminal.
        if base_status == BaseImageStatus.IMPORTING.value:
            logger.info(
                "ProvisionedImage %s: base %s still IMPORTING -- retry",
                provisioned_id, base_image_id,
            )
            return False
        if base_status != BaseImageStatus.READY.value:
            raise TerminalImagerError(
                f"base image {base_image_id} status={base_status} -- "
                f"cannot provision"
            )
        if not base_blob_path or not _BASE_BLOB_PATH_RE.match(base_blob_path):
            raise TerminalImagerError(
                f"base image {base_image_id} blob_path {base_blob_path!r} "
                f"is malformed"
            )
        if not base_sha256 or not _SHA256_RE.match(base_sha256):
            raise TerminalImagerError(
                f"base image {base_image_id} sha256 {base_sha256!r} invalid"
            )
        if payload is None:
            raise TerminalImagerError(
                "fleet_env_payload is null -- API enqueue must populate it"
            )
        try:
            fleet_env_text = bytes(payload).decode("utf-8")
        except UnicodeDecodeError as e:
            raise TerminalImagerError(
                f"fleet_env_payload is not valid utf-8: {e}"
            ) from e

        scratch = _scratch_dir(settings, "build", provisioned_id)
        scratch.mkdir(parents=True, exist_ok=True)

        # Provisioning needs ~10 GiB headroom (xz + raw + output).
        free = await _free_bytes(scratch)
        if free < settings.imager_min_free_bytes:
            logger.warning(
                "ProvisionedImage %s: free space %d below threshold %d -- retry",
                provisioned_id, free, settings.imager_min_free_bytes,
            )
            return False

        # Stage the base image locally.
        staged_base = scratch / "base.img.xz"
        await storage.download_to_file(
            settings.base_image_cache_container,
            base_blob_path,
            staged_base,
        )

        # Defence in depth: blob bytes' SHA256 must still match the row.
        actual_base_sha = await _hash_file(staged_base)
        if actual_base_sha.lower() != base_sha256.lower():
            raise TerminalImagerError(
                f"tenant base blob sha256 drifted: row={base_sha256.lower()}, "
                f"blob={actual_base_sha.lower()}"
            )

        # Run the (synchronous, multi-minute) imager pipeline off the
        # event loop so the dispatcher heartbeat keeps renewing the
        # queue lease.  build_provisioned validates output_name itself
        # and raises ImagerError on bad input or subprocess failure.
        try:
            output_path: Path = await asyncio.to_thread(
                build_provisioned,
                staged_base,
                fleet_env_text,
                scratch,
                output_name,
            )
        except ImagerError as e:
            # ImagerError covers (a) input validation (output_name,
            # fleet_env contents) which is terminal, and (b)
            # subprocess failures which are usually transient.  We
            # treat all ImagerError as terminal because retrying with
            # the same inputs will hit the same failure -- if a
            # subprocess flake is seen we'd rather surface it to the
            # operator than burn 5 retries.
            raise TerminalImagerError(f"imager pipeline failed: {e}") from e

        actual_sha = await _hash_file(output_path)
        actual_size = output_path.stat().st_size

        blob_path = f"{provisioned_id}/{output_name}"
        await storage.upload_local_file(
            settings.provisioned_container,
            blob_path,
            output_path,
            overwrite=True,
        )

        now = _utcnow()
        expires_at = now + timedelta(hours=settings.provisioned_retention_hours)
        async with session_factory() as db:
            row = (await db.execute(
                select(ProvisionedImage).where(
                    ProvisionedImage.id == provisioned_id
                )
            )).scalar_one_or_none()
            if row is None:
                logger.warning(
                    "ProvisionedImage %s row vanished post-upload",
                    provisioned_id,
                )
                return False
            row.output_sha256 = actual_sha.lower()
            row.output_size = actual_size
            row.blob_path = blob_path
            row.built_at = now
            row.expires_at = expires_at
            row.status = ProvisionedImageStatus.READY.value
            row.error_message = ""
            # Secret hygiene: success path also clears the payload.
            row.fleet_env_payload = None
            await db.commit()

        logger.info(
            "ProvisionedImage %s built: %s (%d bytes)",
            provisioned_id, blob_path, actual_size,
        )
        return True
    except TerminalImagerError as e:
        logger.error(
            "ProvisionedImage %s terminal failure: %s", provisioned_id, e,
        )
        await _set_provisioned_terminal(session_factory, provisioned_id, str(e))
        raise
    finally:
        if scratch is not None and scratch.exists():
            shutil.rmtree(scratch, ignore_errors=True)
