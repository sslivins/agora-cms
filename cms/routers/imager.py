"""Browser-driven Pi image provisioning API (Option E, Model B).

This router exposes the operator-facing surface for the imager
pipeline shipped in PRs 1–3:

* :http:get:`/api/imager/fleets` — list the fleet IDs this CMS is
  configured to register devices for (no secrets returned).
* :http:get:`/api/imager/catalog` — live-fetch the upstream
  ``catalog.json`` and return its parsed entries.  Side-effect-free.
* :http:get:`/api/imager/base-images` — list cached
  :class:`shared.models.imager.BaseImage` rows.
* :http:post:`/api/imager/base-images` — enqueue an ``IMAGE_IMPORT``
  job for ``(variant, version)``.  Idempotent against the unique
  constraint: if the row already exists we either return it (READY /
  IMPORTING) or restart it (FAILED).
* :http:delete:`/api/imager/base-images/{id}` — delete a cached
  base image and its blob.  Refused with 409 if any
  ``ProvisionedImage`` references it (FK is RESTRICT).
* :http:post:`/api/imager/build` — enqueue an ``IMAGE_PROVISION``
  job that produces a generic enrollment image carrying
  ``(AGORA_CMS_URL, AGORA_FLEET_ID, AGORA_FLEET_SECRET)``.  No
  per-device API key minting.
* :http:get:`/api/imager/jobs/{job_id}` — poll job status.  Filtered
  to imager job types so non-imager jobs cannot be peeked through
  this endpoint.  Includes the SAS download URL once a successful
  ``IMAGE_PROVISION`` lands.
* :http:get:`/api/imager/download/{job_id}` — 302 to a fresh SAS
  URL for the provisioned image.  Audited.

Identity model is **Model B only**: the image carries fleet
credentials, not per-device credentials.  Each Pi flashed with the
output pairs as a brand-new device via the existing
``/api/devices/register`` HMAC flow on first boot.  Device-targeted
re-flashes (Model A) are tracked as a future feature.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_settings, require_permission
from cms.config import Settings
from cms.database import get_db
from cms.models.user import User
from cms.permissions import IMAGER_BUILD, IMAGER_MANAGE, IMAGER_READ
from cms.services.audit_service import audit_log
from cms.services.imager import is_valid_output_name
from cms.services.imager_settings import (
    CatalogUrlValidationError,
    get_catalog_url,
    set_catalog_url,
    validate_catalog_url,
)
from shared.models.imager import (
    BaseImage,
    BaseImageStatus,
    ProvisionedImage,
    ProvisionedImageStatus,
)
from shared.models.job import Job, JobStatus, JobType
from shared.services.imager_catalog import (
    CatalogError,
    HTTP_TIMEOUT,
    fetch_catalog,
    parse_allowed_hosts,
)
from shared.services.jobs import enqueue_job
from shared.services.storage import get_storage


router = APIRouter(prefix="/api/imager")


_VARIANT_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_FLEET_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# ── Schemas ──────────────────────────────────────────────────────


class FleetOut(BaseModel):
    fleet_id: str


class CatalogEntryOut(BaseModel):
    variant: str
    url: str
    sha256: str
    size_bytes: int | None = None


class CatalogOut(BaseModel):
    ref: str | None = None
    entries: list[CatalogEntryOut]


class BaseImageOut(BaseModel):
    id: uuid.UUID
    variant: str
    version: str
    sha256: str | None
    blob_path: str | None
    size_bytes: int | None
    status: str
    error_message: str
    is_default: bool
    imported_at: datetime | None
    created_at: datetime

    model_config = {"from_attributes": True}


class BaseImageImportBody(BaseModel):
    variant: str = Field(..., min_length=1, max_length=64)
    version: str = Field(..., min_length=1, max_length=64)


class BuildBody(BaseModel):
    base_image_id: uuid.UUID
    fleet_id: str = Field(..., min_length=1, max_length=64)
    output_name: str = Field(..., min_length=1, max_length=255)


class ImagerSettingsOut(BaseModel):
    """Imager runtime settings exposed over the API.

    ``catalog_url`` is ``None`` when no catalog URL has been
    configured -- the UI uses this to disable the "Import from
    catalog" button and show an admin-targeted prompt to configure it.
    """

    catalog_url: str | None = None


class ImagerSettingsUpdateBody(BaseModel):
    catalog_url: str = Field(..., min_length=1, max_length=2048)


class JobStatusOut(BaseModel):
    job_id: uuid.UUID
    type: str
    status: str
    target_id: uuid.UUID
    error_message: str
    created_at: datetime
    completed_at: datetime | None
    # Populated only for terminal-success IMAGE_PROVISION jobs.
    download_url: str | None = None
    output_name: str | None = None
    expires_at: datetime | None = None


# ── Helpers ──────────────────────────────────────────────────────


async def _resolve_catalog_url_or_503(db: AsyncSession) -> str:
    """Return the DB-configured catalog URL or raise 503.

    PR 7 moved the catalog URL from a deploy-time env var to a runtime
    setting an admin can edit at ``PUT /api/imager/settings``.  All
    callers that previously read ``settings.base_image_catalog_url``
    use this helper so a missing setting renders a helpful 503 instead
    of failing later inside the catalog fetch.
    """
    url = await get_catalog_url(db)
    if not url:
        raise HTTPException(
            status_code=503,
            detail=(
                "imager catalog URL is not configured; "
                "set it via PUT /api/imager/settings"
            ),
        )
    return url


def _allowlist(settings: Settings) -> set[str]:
    return parse_allowed_hosts(settings.base_image_allowed_hosts)


def _base_image_to_out(bi: BaseImage) -> BaseImageOut:
    return BaseImageOut.model_validate(bi)


def _resolve_catalog_entry(
    catalog: dict[str, Any], variant: str, version: str
) -> dict[str, Any]:
    """Return the catalog entry for ``variant`` validated against ``version``.

    Raises :class:`HTTPException` 404 if no entry, 422 if ``ref``
    mismatches, 422 if entry is missing url/sha256.
    """
    ref = catalog.get("ref")
    if ref and ref != version:
        raise HTTPException(
            status_code=422,
            detail=f"catalog ref {ref!r} does not match requested version {version!r}",
        )
    variants = catalog.get("variants") or {}
    entry = variants.get(variant)
    if not isinstance(entry, dict):
        raise HTTPException(
            status_code=404,
            detail=f"catalog has no entry for variant {variant!r}",
        )
    url = entry.get("url")
    sha256 = entry.get("sha256")
    if not url or not sha256:
        raise HTTPException(
            status_code=422,
            detail=f"catalog entry for {variant!r} missing url or sha256",
        )
    return entry


def _derive_device_ws_url(base_url: str) -> str:
    """Derive the device WebSocket URL the firmware connects to.

    Maps ``https://host[:port]`` → ``wss://host[:port]/ws/device``
    (and ``http://`` → ``ws://`` for local dev).  This is the value
    baked into ``agora-fleet.env`` as ``AGORA_CMS_URL``; the firmware
    passes it directly to ``websockets.connect()`` and derives the
    HTTPS API base by swapping the scheme back.

    The input must be a clean origin URL (scheme + host[:port], no
    path/query/fragment) — anything else is rejected so we never
    silently bake a half-correct URL into a Pi image.

    Raises :class:`ValueError` on empty input, missing host, an
    unsupported scheme, or any path/query/fragment.
    """
    if not base_url:
        raise ValueError("base_url is empty")
    parsed = urlparse(base_url.rstrip("/"))
    if parsed.scheme not in ("http", "https", "ws", "wss"):
        raise ValueError(
            f"base_url {base_url!r} has unsupported scheme {parsed.scheme!r}; "
            "expected http(s) or ws(s)"
        )
    if not parsed.netloc:
        raise ValueError(f"base_url {base_url!r} has no host component")
    if parsed.path or parsed.params or parsed.query or parsed.fragment:
        raise ValueError(
            f"base_url {base_url!r} must be origin only "
            "(scheme + host[:port], no path/query/fragment)"
        )
    ws_scheme = "wss" if parsed.scheme in ("https", "wss") else "ws"
    return urlunparse((ws_scheme, parsed.netloc, "/ws/device", "", "", ""))


def _fleet_env_payload(
    cms_url: str, fleet_id: str, fleet_secret: str, device_transport: str
) -> bytes:
    """Render the ``agora-fleet.env`` body the worker will inject.

    Plaintext on purpose -- see model docstring for rationale.  Order
    matters only for human readability; the firmware reads via
    standard ``KEY=VALUE`` env parsing.

    ``device_transport`` is the CMS-side mode (``local`` or ``wps``).
    It is mapped to the firmware-side value (``direct`` or ``wps``)
    and emitted as ``AGORA_CMS_TRANSPORT`` so the Pi opens the right
    transport at boot.  In ``wps`` mode the firmware reuses the same
    ``cms_url`` (only its scheme+netloc) to derive the API base for
    ``/api/devices/.../connect-token``; the ``/ws/device`` path is
    ignored, so the same URL form works for both modes.
    """
    fw_transport = "direct" if device_transport == "local" else device_transport
    return (
        f"AGORA_CMS_URL={cms_url}\n"
        f"AGORA_CMS_TRANSPORT={fw_transport}\n"
        f"AGORA_FLEET_ID={fleet_id}\n"
        f"AGORA_FLEET_SECRET={fleet_secret}\n"
    ).encode("utf-8")


# ── Endpoints ────────────────────────────────────────────────────


@router.get("/fleets", response_model=list[FleetOut])
async def list_fleets(
    user: User = Depends(require_permission(IMAGER_READ)),
    settings: Settings = Depends(get_settings),
) -> list[FleetOut]:
    """Return the fleet IDs this CMS can register devices for.

    Source of truth is :attr:`Settings.fleet_register_secrets`
    (env-configured).  Secrets themselves are never returned.
    """
    fleets = sorted((settings.fleet_register_secrets or {}).keys())
    return [FleetOut(fleet_id=f) for f in fleets]


@router.get("/settings", response_model=ImagerSettingsOut)
async def get_imager_settings(
    user: User = Depends(require_permission(IMAGER_READ)),
    db: AsyncSession = Depends(get_db),
) -> ImagerSettingsOut:
    """Return the current imager runtime settings.

    Always 200; ``catalog_url`` may be ``None`` when unset, which the
    UI uses to disable the "Import from catalog" button.
    """
    url = await get_catalog_url(db)
    return ImagerSettingsOut(catalog_url=url)


@router.put("/settings", response_model=ImagerSettingsOut)
async def update_imager_settings(
    body: ImagerSettingsUpdateBody,
    request: Request,
    user: User = Depends(require_permission(IMAGER_MANAGE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ImagerSettingsOut:
    """Set the imager catalog URL.

    Validates that the URL is https and the host is in
    ``BASE_IMAGE_ALLOWED_HOSTS``.  Audited.
    """
    try:
        cleaned = validate_catalog_url(
            body.catalog_url, settings.base_image_allowed_hosts
        )
    except CatalogUrlValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e
    stored = await set_catalog_url(db, cleaned)
    await audit_log(
        db,
        user=user,
        action="imager.settings.update",
        resource_type="imager_settings",
        resource_id="catalog_url",
        details={"catalog_url": stored},
        request=request,
    )
    await db.commit()
    return ImagerSettingsOut(catalog_url=stored)


@router.get("/catalog", response_model=CatalogOut)
async def get_catalog(
    user: User = Depends(require_permission(IMAGER_MANAGE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> CatalogOut:
    """Live-fetch and parse the upstream catalog manifest.

    Side-effect-free; intended for the UI to populate the
    "import a new base image" picker without writing anything to DB.
    """
    catalog_url = await _resolve_catalog_url_or_503(db)
    allowlist = _allowlist(settings)
    if not allowlist:
        raise HTTPException(
            status_code=503,
            detail="BASE_IMAGE_ALLOWED_HOSTS is empty; refusing catalog fetch",
        )
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            doc = await fetch_catalog(catalog_url, allowlist, client)
    except CatalogError as e:
        raise HTTPException(status_code=502, detail=f"catalog: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"catalog fetch failed: {e}") from e

    entries: list[CatalogEntryOut] = []
    for variant, payload in (doc.get("variants") or {}).items():
        if not isinstance(payload, dict):
            continue
        url = payload.get("url")
        sha256 = payload.get("sha256")
        if not url or not sha256:
            continue
        entries.append(CatalogEntryOut(
            variant=variant,
            url=url,
            sha256=sha256,
            size_bytes=payload.get("size_bytes"),
        ))
    return CatalogOut(ref=doc.get("ref"), entries=entries)


@router.get("/base-images", response_model=list[BaseImageOut])
async def list_base_images(
    user: User = Depends(require_permission(IMAGER_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[BaseImageOut]:
    """List cached base images, newest first."""
    result = await db.execute(
        select(BaseImage).order_by(BaseImage.created_at.desc())
    )
    return [_base_image_to_out(bi) for bi in result.scalars().all()]


@router.post("/base-images", response_model=BaseImageOut)
async def import_base_image(
    body: BaseImageImportBody,
    request: Request,
    user: User = Depends(require_permission(IMAGER_MANAGE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BaseImageOut:
    """Resolve ``(variant, version)`` against the catalog and enqueue an import.

    Behaviour matrix on the unique ``(variant, version)`` constraint:

    * No existing row → create + enqueue.
    * Existing READY → return existing row, do nothing.
    * Existing IMPORTING → return existing row, do nothing.
    * Existing FAILED → reset to IMPORTING, restamp catalog
      coordinates, re-enqueue.
    """
    if not _VARIANT_RE.match(body.variant):
        raise HTTPException(status_code=422, detail="invalid variant")
    if not _VERSION_RE.match(body.version):
        raise HTTPException(status_code=422, detail="invalid version")

    # Live-resolve the catalog entry so we stamp source_url + expected_sha256
    # at enqueue time.  This eliminates the TOCTOU window between admin
    # click and worker pickup (worker uses the stamped values, not the
    # mutable catalog).
    catalog_url = await _resolve_catalog_url_or_503(db)
    allowlist = _allowlist(settings)
    try:
        async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as client:
            doc = await fetch_catalog(catalog_url, allowlist, client)
    except CatalogError as e:
        raise HTTPException(status_code=502, detail=f"catalog: {e}") from e
    except httpx.HTTPError as e:
        raise HTTPException(status_code=502, detail=f"catalog fetch failed: {e}") from e
    entry = _resolve_catalog_entry(doc, body.variant, body.version)

    existing = await db.execute(
        select(BaseImage).where(
            BaseImage.variant == body.variant,
            BaseImage.version == body.version,
        )
    )
    bi = existing.scalar_one_or_none()
    if bi is not None and bi.status != BaseImageStatus.FAILED.value:
        # READY or IMPORTING: idempotent no-op.
        return _base_image_to_out(bi)

    if bi is None:
        bi = BaseImage(
            variant=body.variant,
            version=body.version,
            source_url=entry["url"],
            expected_sha256=entry["sha256"],
            size_bytes=entry.get("size_bytes"),
            imported_by=user.id,
            status=BaseImageStatus.IMPORTING.value,
        )
        db.add(bi)
        try:
            await db.flush()
        except IntegrityError:
            # Lost the race against a concurrent insert; reload winner.
            await db.rollback()
            again = await db.execute(
                select(BaseImage).where(
                    BaseImage.variant == body.variant,
                    BaseImage.version == body.version,
                )
            )
            bi = again.scalar_one()
            return _base_image_to_out(bi)
    else:
        # FAILED: restart.
        bi.source_url = entry["url"]
        bi.expected_sha256 = entry["sha256"]
        bi.size_bytes = entry.get("size_bytes")
        bi.status = BaseImageStatus.IMPORTING.value
        bi.error_message = ""
        bi.sha256 = None
        bi.blob_path = None
        bi.imported_at = None
        bi.imported_by = user.id
        await db.flush()

    await audit_log(
        db, user=user, action="imager.base_image.import",
        resource_type="base_image", resource_id=str(bi.id),
        details={
            "variant": bi.variant,
            "version": bi.version,
            "source_url": bi.source_url,
            "expected_sha256": bi.expected_sha256,
        },
        request=request,
    )

    # ``enqueue_job`` commits the surrounding transaction, persisting
    # both the BaseImage row and the audit entry.
    await enqueue_job(db, JobType.IMAGE_IMPORT, bi.id)
    await db.refresh(bi)
    return _base_image_to_out(bi)


@router.delete("/base-images/{base_image_id}", status_code=204)
async def delete_base_image(
    base_image_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(IMAGER_MANAGE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    """Delete a cached base image (DB row + tenant blob).

    Refuses with 409 if any ``ProvisionedImage`` row references this
    base image.  The DB FK is ``RESTRICT`` so a relaxed implementation
    would still surface that error -- we check eagerly for a clearer
    message.
    """
    bi = await db.get(BaseImage, base_image_id)
    if bi is None:
        raise HTTPException(status_code=404, detail="base image not found")

    ref_count = await db.scalar(
        select(func.count()).select_from(ProvisionedImage).where(
            ProvisionedImage.base_image_id == base_image_id
        )
    )
    if ref_count and int(ref_count) > 0:
        raise HTTPException(
            status_code=409,
            detail=(
                f"cannot delete: {int(ref_count)} provisioned image(s) "
                "reference this base image"
            ),
        )

    blob_path = bi.blob_path
    container = settings.base_image_cache_container

    await db.delete(bi)
    await audit_log(
        db, user=user, action="imager.base_image.delete",
        resource_type="base_image", resource_id=str(base_image_id),
        details={"variant": bi.variant, "version": bi.version, "blob_path": blob_path},
        request=request,
    )
    await db.commit()

    # Best-effort blob cleanup.  We've already committed the delete
    # so a blob-cleanup failure should not roll back the row removal;
    # an admin-initiated retry will succeed once Azure recovers.
    if blob_path:
        try:
            storage = get_storage()
            if hasattr(storage, "delete_blob"):
                await storage.delete_blob(container, blob_path)  # type: ignore[attr-defined]
        except Exception:
            # Logged via audit_log already; we don't fail the API.
            pass


@router.post("/build", response_model=JobStatusOut)
async def build_provisioned_image(
    body: BuildBody,
    request: Request,
    user: User = Depends(require_permission(IMAGER_BUILD)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JobStatusOut:
    """Enqueue an ``IMAGE_PROVISION`` job and return the job handle.

    Validates that:

    * ``output_name`` is a safe ``.img.xz`` basename;
    * ``fleet_id`` is configured on this CMS (resolves to a secret);
    * ``base_image_id`` exists and is READY;
    * a CMS public base URL is configured (the firmware needs an
      absolute URL to call ``/api/devices/register``).
    """
    if not is_valid_output_name(body.output_name):
        raise HTTPException(
            status_code=422,
            detail="output_name must be a basename like 'foo.img.xz'",
        )
    if not _FLEET_ID_RE.match(body.fleet_id):
        raise HTTPException(status_code=422, detail="invalid fleet_id")

    secret = (settings.fleet_register_secrets or {}).get(body.fleet_id)
    if not secret:
        raise HTTPException(
            status_code=404,
            detail=f"fleet_id {body.fleet_id!r} is not configured on this CMS",
        )

    if settings.device_transport not in ("local", "wps"):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Image builds require AGORA_CMS_DEVICE_TRANSPORT to be "
                f"'local' or 'wps'; this CMS is configured for "
                f"{settings.device_transport!r}."
            ),
        )

    cms_base = (settings.base_url or "").rstrip("/")
    if not cms_base:
        raise HTTPException(
            status_code=503,
            detail=(
                "AGORA_CMS_BASE_URL is not configured. Set it to the public URL "
                "of this CMS (e.g. https://agora.example.com) — image builds "
                "embed it in the Pi's fleet env so the device can reach "
                "/ws/device and /api/devices/register."
            ),
        )
    try:
        cms_url = _derive_device_ws_url(cms_base)
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail=f"AGORA_CMS_BASE_URL is invalid ({exc}); expected an absolute URL like https://agora.example.com",
        )

    bi = await db.get(BaseImage, body.base_image_id)
    if bi is None:
        raise HTTPException(status_code=404, detail="base image not found")
    if bi.status != BaseImageStatus.READY.value:
        raise HTTPException(
            status_code=409,
            detail=f"base image is not ready (status={bi.status})",
        )

    payload = _fleet_env_payload(
        cms_url, body.fleet_id, secret, settings.device_transport
    )

    pi = ProvisionedImage(
        base_image_id=bi.id,
        output_name=body.output_name,
        fleet_env_payload=payload,
        fleet_id=body.fleet_id,
        built_by=user.id,
        status=ProvisionedImageStatus.PROVISIONING.value,
    )
    db.add(pi)
    await db.flush()

    await audit_log(
        db, user=user, action="imager.build",
        resource_type="provisioned_image", resource_id=str(pi.id),
        # NEVER include the secret here.
        details={
            "base_image_id": str(bi.id),
            "variant": bi.variant,
            "version": bi.version,
            "fleet_id": body.fleet_id,
            "output_name": body.output_name,
        },
        request=request,
    )

    job_id = await enqueue_job(db, JobType.IMAGE_PROVISION, pi.id)
    await db.refresh(pi)

    job = await db.get(Job, job_id)
    assert job is not None
    return JobStatusOut(
        job_id=job.id,
        type=job.type.value if hasattr(job.type, "value") else str(job.type),
        status=job.status.value if hasattr(job.status, "value") else str(job.status),
        target_id=job.target_id,
        error_message=job.error_message,
        created_at=job.created_at,
        completed_at=job.completed_at,
        output_name=pi.output_name,
    )


_IMAGER_JOB_TYPES = (JobType.IMAGE_IMPORT, JobType.IMAGE_PROVISION)


@router.get("/jobs/{job_id}", response_model=JobStatusOut)
async def get_job_status(
    job_id: uuid.UUID,
    user: User = Depends(require_permission(IMAGER_READ)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> JobStatusOut:
    """Return the status of an imager job.

    Filtered to imager job types so this endpoint cannot be used to
    peek at unrelated CMS jobs (transcode, capture, etc.).
    """
    job = await db.get(Job, job_id)
    if job is None or job.type not in _IMAGER_JOB_TYPES:
        raise HTTPException(status_code=404, detail="imager job not found")

    out = JobStatusOut(
        job_id=job.id,
        type=job.type.value if hasattr(job.type, "value") else str(job.type),
        status=job.status.value if hasattr(job.status, "value") else str(job.status),
        target_id=job.target_id,
        error_message=job.error_message,
        created_at=job.created_at,
        completed_at=job.completed_at,
    )

    if job.type == JobType.IMAGE_PROVISION and job.status == JobStatus.DONE:
        pi = await db.get(ProvisionedImage, job.target_id)
        if pi is not None:
            out.output_name = pi.output_name
            out.expires_at = pi.expires_at
            if (
                pi.status == ProvisionedImageStatus.READY.value
                and pi.blob_path
            ):
                storage = get_storage()
                out.download_url = storage.generate_blob_sas_url(
                    settings.provisioned_container,
                    pi.blob_path,
                    settings.imager_sas_ttl_hours,
                )

    return out


@router.get("/download/{job_id}")
async def download_provisioned(
    job_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(IMAGER_BUILD)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RedirectResponse:
    """Return a 302 to a fresh SAS URL for the provisioned image.

    Audited.  404s when the job is unknown, not an ``IMAGE_PROVISION``,
    not yet succeeded, or whose row is no longer ``READY`` (e.g. the
    24 h Azure lifecycle policy has expired the blob and the row is
    now ``EXPIRED``).
    """
    job = await db.get(Job, job_id)
    if job is None or job.type != JobType.IMAGE_PROVISION:
        raise HTTPException(status_code=404, detail="imager job not found")
    if job.status != JobStatus.DONE:
        raise HTTPException(
            status_code=409,
            detail=f"job is not ready (status={job.status.value if hasattr(job.status, 'value') else job.status})",
        )

    pi = await db.get(ProvisionedImage, job.target_id)
    if pi is None or pi.status != ProvisionedImageStatus.READY.value or not pi.blob_path:
        raise HTTPException(status_code=404, detail="provisioned image not available")

    # Verify the blob actually exists before handing back a SAS so an
    # expired-and-cleaned-up artifact 404s here cleanly rather than
    # the operator's browser landing on a generic Azure 404.
    storage = get_storage()
    if not await storage.blob_exists(settings.provisioned_container, pi.blob_path):
        raise HTTPException(status_code=404, detail="provisioned image blob is gone")

    url = storage.generate_blob_sas_url(
        settings.provisioned_container,
        pi.blob_path,
        settings.imager_sas_ttl_hours,
    )

    await audit_log(
        db, user=user, action="imager.download",
        resource_type="provisioned_image", resource_id=str(pi.id),
        details={
            "job_id": str(job.id),
            "base_image_id": str(pi.base_image_id),
            "fleet_id": pi.fleet_id,
            "output_name": pi.output_name,
        },
        request=request,
    )
    await db.commit()

    return RedirectResponse(url=url, status_code=302)
