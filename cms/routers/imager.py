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
  ``(AGORA_CMS_URL, AGORA_FLEET_ID, AGORA_FLEET_SECRET_HEX)``.  No
  per-device API key minting.
* :http:get:`/api/imager/jobs/{job_id}` — poll job status.  Filtered
  to imager job types so non-imager jobs cannot be peeked through
  this endpoint.  Includes the SAS download URL once a successful
  ``IMAGE_PROVISION`` lands.
* :http:get:`/api/imager/download/{job_id}` — 302 to a fresh SAS
  URL for the provisioned image.  Audited.
* :http:get:`/api/imager/download-url/{job_id}` — return that same
  fresh SAS URL as JSON ``{"url": ...}`` so the UI's "Copy
  download link" action can hand the raw URL to ``wget`` / ``curl``
  without going through a browser redirect.  Audited.

Identity model is **Model B only**: the image carries fleet
credentials, not per-device credentials.  Each Pi flashed with the
output pairs as a brand-new device via the existing
``/api/devices/register`` HMAC flow on first boot.  Device-targeted
re-flashes (Model A) are tracked as a future feature.
"""

from __future__ import annotations

import base64
import binascii
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse, urlunparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_settings, require_permission
from cms.config import Settings
from cms.database import get_db
from cms.models.user import User
from cms.permissions import IMAGER_BUILD, IMAGER_MANAGE, IMAGER_READ
from cms.services import fleet_registry
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


class FleetCreateBody(BaseModel):
    fleet_id: str = Field(..., min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=255)


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
    # In-flight progress, populated for ``IMPORTING`` rows from the
    # latest in-flight :class:`Job` of type ``IMAGE_IMPORT``.  Both
    # default to "no progress yet" so READY / FAILED rows are unaffected.
    progress_stage: str = ""
    progress_pct: int | None = None

    model_config = {"from_attributes": True}


class BaseImageImportBody(BaseModel):
    variant: str = Field(..., min_length=1, max_length=64)
    version: str = Field(..., min_length=1, max_length=64)


class ProvisionedImageOut(BaseModel):
    """Read model for the built-images list view.

    ``base_image_id`` may be ``None`` if the cached base was deleted
    after this build was produced -- the denormalized ``base_variant``
    and ``base_version`` snapshot columns preserve the audit trail.

    ``download_job_id`` lets the UI build the link to
    ``/api/imager/download/{job_id}`` (which 302s to a freshly-minted
    SAS URL on each click -- the SAS embedded at build time is not
    used).  ``None`` only for legacy rows pre-dating the
    ``provisioning_job_id`` column; UI should disable Download.

    ``wifi_ssid`` / ``wifi_psk`` are the cleartext WiFi creds baked
    into the image, surfaced so the operator can recover them via the
    Built Images tooltip.  Both ``None`` means the image carries no
    wifi creds.
    """

    id: uuid.UUID
    output_name: str
    fleet_id: str | None
    base_image_id: uuid.UUID | None
    base_variant: str | None
    base_version: str | None
    status: str
    blob_path: str | None
    output_size: int | None
    download_job_id: uuid.UUID | None
    created_at: datetime
    built_at: datetime | None
    expires_at: datetime | None
    wifi_ssid: str | None = None
    wifi_psk: str | None = None
    # In-flight progress, populated for ``PROVISIONING`` rows from the
    # latest in-flight :class:`Job` of type ``IMAGE_PROVISION``.  Both
    # default to "no progress yet" so READY / FAILED rows are unaffected.
    progress_stage: str = ""
    progress_pct: int | None = None

    model_config = {"from_attributes": True}


class BuildBody(BaseModel):
    base_image_id: uuid.UUID
    fleet_id: str = Field(..., min_length=1, max_length=64)
    output_name: str = Field(..., min_length=1, max_length=255)
    # Optional WiFi credentials. Both must be provided or both omitted
    # (validated below). SSID limit per IEEE 802.11; PSK limit per WPA
    # passphrase (8-63 ASCII) or 64-hex pre-shared key. We don't
    # enforce 8-63 for passphrase here because NetworkManager accepts
    # both forms transparently and we'd rather not reject the 64-hex
    # form for users who paste a pre-computed PSK.
    wifi_ssid: str | None = Field(default=None, min_length=1, max_length=32)
    wifi_psk: str | None = Field(default=None, min_length=8, max_length=64)

    @model_validator(mode="after")
    def _check_wifi_pair(self) -> "BuildBody":
        # Both-or-neither: pre-validate at the schema layer so the
        # build endpoint can assume a coherent pair.  Using
        # @model_validator (vs model_post_init) so pydantic surfaces
        # the failure as 422 instead of a generic 500.
        if (self.wifi_ssid is None) != (self.wifi_psk is None):
            raise ValueError(
                "wifi_ssid and wifi_psk must be provided together or both omitted"
            )
        return self


class SoftplayerCredentialsBody(BaseModel):
    """Request body for ``POST /api/imager/softplayer-credentials``.

    Just a fleet id -- the softplayer is the .env-file analogue of the Pi
    .img.xz build, so there's no base image, output filename, or wifi to
    plumb through.
    """

    fleet_id: str = Field(..., min_length=1, max_length=64)


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
    # Coarse progress fields populated by the imager worker handlers.
    # ``progress_stage`` is a short label ("downloading", "building",
    # "uploading", ...).  ``progress_pct`` is an optional 0-100
    # estimate; ``None`` means "unknown -- show indeterminate UI".
    progress_stage: str = ""
    progress_pct: int | None = None
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


_IN_FLIGHT_JOB_STATUSES = (JobStatus.PENDING, JobStatus.PROCESSING)


async def _latest_in_flight_progress(
    db: AsyncSession,
    job_type: JobType,
    target_ids: list[uuid.UUID],
) -> dict[uuid.UUID, tuple[str, int | None]]:
    """Return ``{target_id: (progress_stage, progress_pct)}`` for in-flight jobs.

    One DB round-trip regardless of how many target IDs are passed.
    For each target, the *latest-by-created_at* in-flight job (PENDING
    or PROCESSING) wins -- a finished previous attempt does not bleed
    its stage into the surface.  Targets without an in-flight job are
    omitted from the result entirely.
    """
    if not target_ids:
        return {}
    rows = (
        await db.execute(
            select(Job.target_id, Job.progress_stage, Job.progress_pct, Job.created_at)
            .where(
                Job.type == job_type,
                Job.status.in_(_IN_FLIGHT_JOB_STATUSES),
                Job.target_id.in_(target_ids),
            )
            .order_by(Job.target_id, Job.created_at.desc())
        )
    ).all()
    out: dict[uuid.UUID, tuple[str, int | None]] = {}
    for target_id, stage, pct, _created_at in rows:
        # Sorted target_id ASC, created_at DESC -- first row per target
        # is the latest in-flight job.
        if target_id not in out:
            out[target_id] = (stage or "", pct)
    return out


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
    cms_url: str,
    fleet_id: str,
    fleet_secret: bytes,
    device_transport: str,
    *,
    wifi_ssid: str | None = None,
    wifi_psk: str | None = None,
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

    ``fleet_secret`` is the **raw HMAC bytes** (typically 32 bytes
    from ``secrets.token_bytes``).  The firmware-side
    ``agora-fleet-provision.sh`` allow-lists the key
    ``AGORA_FLEET_SECRET_HEX`` and hex-decodes the value, so we
    hex-encode here.  The 2026-05-06 incident on Pi 192.168.1.100
    was caused by writing the key as ``AGORA_FLEET_SECRET=<b64>``,
    which the firmware silently dropped (wrong key name, wrong
    encoding) leaving the device unable to authenticate.

    When ``wifi_ssid`` and ``wifi_psk`` are both provided, additional
    ``AGORA_WIFI_SSID`` / ``AGORA_WIFI_PASS`` lines are appended.  The
    firmware-side allow-list pins these exact key names (NOT
    ``AGORA_WIFI_PASSPHRASE``) and the provisioner script writes a
    NetworkManager ``.nmconnection`` file from them on first boot.
    Either both must be set or both must be omitted; the build endpoint
    enforces this before calling.  Devices that have no wifi hardware
    silently no-op the connection file (firmware probes
    ``/sys/class/ieee80211``), so leftover wifi env on a wired-only Pi
    is harmless.
    """
    fw_transport = "direct" if device_transport == "local" else device_transport
    secret_hex = fleet_secret.hex()
    out = (
        f"AGORA_CMS_URL={cms_url}\n"
        f"AGORA_CMS_TRANSPORT={fw_transport}\n"
        f"AGORA_FLEET_ID={fleet_id}\n"
        f"AGORA_FLEET_SECRET_HEX={secret_hex}\n"
    )
    if wifi_ssid and wifi_psk:
        out += f"AGORA_WIFI_SSID={wifi_ssid}\n"
        out += f"AGORA_WIFI_PASS={wifi_psk}\n"
    return out.encode("utf-8")


# ── Endpoints ────────────────────────────────────────────────────


@router.get("/fleets", response_model=list[FleetOut])
async def list_fleets(
    user: User = Depends(require_permission(IMAGER_READ)),
    db: AsyncSession = Depends(get_db),
) -> list[FleetOut]:
    """Return the fleet IDs this CMS can register devices for.

    Source of truth is the ``fleets`` table (see
    :mod:`cms.services.fleet_registry`). Secrets themselves are
    never returned.
    """
    rows = await fleet_registry.list_active_fleets(db)
    return [FleetOut(fleet_id=f.fleet_id) for f in rows]


@router.post(
    "/fleets",
    response_model=FleetOut,
    status_code=201,
)
async def create_fleet_endpoint(
    body: FleetCreateBody,
    user: User = Depends(require_permission(IMAGER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> FleetOut:
    """Create a new fleet.

    The HMAC secret is server-generated (32 bytes from
    ``secrets.token_bytes``) and is **never** returned by this or
    any other endpoint -- once the row is written, only freshly-built
    images carry it. To replace the secret, delete the fleet and
    re-create it.

    Returns 409 if an active fleet with the given ``fleet_id``
    already exists.
    """
    if not _FLEET_ID_RE.match(body.fleet_id):
        raise HTTPException(status_code=422, detail="invalid fleet_id")
    try:
        row = await fleet_registry.create_fleet(
            db,
            fleet_id=body.fleet_id,
            description=body.description,
            created_by=user.id,
        )
    except fleet_registry.FleetAlreadyExists as exc:
        raise HTTPException(
            status_code=409,
            detail=f"fleet_id {body.fleet_id!r} already exists",
        ) from exc
    await audit_log(
        db,
        user=user,
        action="imager.fleet.create",
        resource_type="fleet",
        resource_id=str(row.id),
        details={"fleet_id": row.fleet_id},
    )
    await db.commit()
    return FleetOut(fleet_id=row.fleet_id)


@router.delete("/fleets/{fleet_id}", status_code=204)
async def delete_fleet_endpoint(
    fleet_id: str,
    user: User = Depends(require_permission(IMAGER_MANAGE)),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Soft-delete a fleet.

    Idempotent: returns 204 even if the fleet was already gone (the
    UI's "Delete" button shouldn't 404 on a stale list view).

    The row is soft-deleted (``deleted_at`` set) so existing
    ``provisioned_images.fleet_id`` audit values still resolve to a
    name in history views. Already-flashed Pis using this fleet's
    HMAC will be rejected with 401 from /register on next attempt.
    """
    if not _FLEET_ID_RE.match(fleet_id):
        raise HTTPException(status_code=422, detail="invalid fleet_id")
    deleted = await fleet_registry.delete_fleet(db, fleet_id)
    if deleted:
        await audit_log(
            db,
            user=user,
            action="imager.fleet.delete",
            resource_type="fleet",
            resource_id=fleet_id,
            details={"fleet_id": fleet_id},
        )
    await db.commit()
    return None


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
    bases = list(result.scalars().all())
    importing_ids = [
        bi.id for bi in bases if bi.status == BaseImageStatus.IMPORTING.value
    ]
    progress = await _latest_in_flight_progress(
        db, JobType.IMAGE_IMPORT, importing_ids
    )
    out: list[BaseImageOut] = []
    for bi in bases:
        item = _base_image_to_out(bi)
        info = progress.get(bi.id)
        if info is not None:
            item.progress_stage = info[0]
            item.progress_pct = info[1]
        out.append(item)
    return out


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

    Built ``.img.xz`` artifacts are fully self-contained -- the imager
    pipeline embeds the OS into a fresh blob -- so removing a cached
    base image cannot break any existing build.  The
    ``provisioned_images.base_image_id`` FK is ``ON DELETE SET NULL``
    (with denormalized ``base_variant`` / ``base_version`` snapshot
    columns preserving the audit trail), so we can drop the row
    unconditionally here.
    """
    bi = await db.get(BaseImage, base_image_id)
    if bi is None:
        raise HTTPException(status_code=404, detail="base image not found")

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

    # FOR SHARE lock — serialises against a concurrent
    # ``DELETE /api/imager/fleets/{id}`` (which takes FOR UPDATE on
    # the same row). See ``cms.services.fleet_registry`` module
    # docstring for the full locking convention.
    fleet = await fleet_registry.get_fleet_for_build(db, body.fleet_id)
    if fleet is None:
        raise HTTPException(
            status_code=404,
            detail=f"fleet_id {body.fleet_id!r} is not configured on this CMS",
        )
    try:
        secret = base64.b64decode(fleet.secret_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"fleet {body.fleet_id!r} has a misconfigured secret",
        ) from exc

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

    try:
        payload = _fleet_env_payload(
            cms_url,
            body.fleet_id,
            secret,
            settings.device_transport,
            wifi_ssid=body.wifi_ssid,
            wifi_psk=body.wifi_psk,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"fleet {body.fleet_id!r} payload could not be rendered: "
                f"{exc}"
            ),
        ) from exc

    pi = ProvisionedImage(
        base_image_id=bi.id,
        # Audit-only snapshot: preserves "which base did this build
        # come from?" if the base image is deleted later.
        base_variant=bi.variant,
        base_version=bi.version,
        output_name=body.output_name,
        fleet_env_payload=payload,
        fleet_id=body.fleet_id,
        # Persist wifi creds so the Built Images tooltip can show them
        # after the build completes (these survive the
        # ``fleet_env_payload`` clear on terminal success).
        wifi_ssid=body.wifi_ssid,
        wifi_psk=body.wifi_psk,
        built_by=user.id,
        status=ProvisionedImageStatus.PROVISIONING.value,
    )
    db.add(pi)
    await db.flush()

    await audit_log(
        db, user=user, action="imager.build",
        resource_type="provisioned_image", resource_id=str(pi.id),
        # NEVER include the secret here. Also intentionally omit wifi_psk;
        # the SSID alone is fine to include for trace usefulness.
        details={
            "base_image_id": str(bi.id),
            "variant": bi.variant,
            "version": bi.version,
            "fleet_id": body.fleet_id,
            "output_name": body.output_name,
            "wifi_ssid": body.wifi_ssid,
        },
        request=request,
    )

    job_id = await enqueue_job(db, JobType.IMAGE_PROVISION, pi.id)
    # Denormalize the producing job onto the row so the list
    # endpoint can surface ``download_job_id`` without a reverse
    # join through ``jobs.target_id`` on every list call.  Note:
    # ``enqueue_job`` already committed the surrounding transaction,
    # so we need our own commit to persist this update.
    pi.provisioning_job_id = job_id
    await db.commit()
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
        progress_stage=job.progress_stage or "",
        progress_pct=job.progress_pct,
        output_name=pi.output_name,
    )


@router.post("/softplayer-credentials")
async def build_softplayer_credentials(
    body: SoftplayerCredentialsBody,
    request: Request,
    user: User = Depends(require_permission(IMAGER_BUILD)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> Response:
    """Render and return the ``softplayer.env`` body for a given fleet.

    The Windows-native ``agora-softplayer`` is bootstrap-v2-only: it
    authenticates to the CMS via the same fleet-HMAC pairing flow Pis
    use in production. Operators provision a softplayer by downloading
    this env file from the Imager tab and dropping it next to the .exe
    (or under ``%LOCALAPPDATA%\\agora-softplayer\\``).

    The output mirrors :func:`_fleet_env_payload`'s ``agora-fleet.env``
    body baked into Pi images -- same keys, same ``AGORA_FLEET_SECRET_HEX``
    encoding -- just delivered as a downloadable text file instead of
    inside an ``.img.xz``. The softplayer CLI consumes the same
    ``KEY=VALUE`` format.

    Validates that:

    * ``fleet_id`` matches ``_FLEET_ID_RE`` and is configured on this CMS
      (resolves to a secret).
    * ``settings.device_transport`` is ``local`` or ``wps``.
    * ``settings.base_url`` is set (the firmware needs an absolute URL).

    Audited via ``imager.softplayer_credentials.download``. No
    ``ProvisionedImage`` row is written -- the env file is ephemeral and
    regeneratable from the fleet at any time; revoking access means
    deleting the fleet (which invalidates the HMAC for every previously
    downloaded file).
    """
    if not _FLEET_ID_RE.match(body.fleet_id):
        raise HTTPException(status_code=422, detail="invalid fleet_id")

    # FOR SHARE lock -- same locking contract as the Pi /build endpoint.
    # Serialises against a concurrent ``DELETE /api/imager/fleets/{id}``.
    fleet = await fleet_registry.get_fleet_for_build(db, body.fleet_id)
    if fleet is None:
        raise HTTPException(
            status_code=404,
            detail=f"fleet_id {body.fleet_id!r} is not configured on this CMS",
        )
    try:
        secret = base64.b64decode(fleet.secret_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"fleet {body.fleet_id!r} has a misconfigured secret",
        ) from exc

    if settings.device_transport not in ("local", "wps"):
        raise HTTPException(
            status_code=503,
            detail=(
                f"Softplayer credentials require AGORA_CMS_DEVICE_TRANSPORT to be "
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
                "of this CMS (e.g. https://agora.example.com) -- softplayer "
                "credentials embed it so the device can reach /ws/device and "
                "/api/devices/register."
            ),
        )
    try:
        cms_url = _derive_device_ws_url(cms_base)
    except ValueError as exc:
        raise HTTPException(
            status_code=503,
            detail=(
                f"AGORA_CMS_BASE_URL is invalid ({exc}); expected an absolute URL "
                "like https://agora.example.com"
            ),
        ) from exc

    payload = _fleet_env_payload(
        cms_url,
        body.fleet_id,
        secret,
        settings.device_transport,
    )

    await audit_log(
        db,
        user=user,
        action="imager.softplayer_credentials.download",
        resource_type="fleet",
        resource_id=str(fleet.id),
        # NEVER include the secret here. fleet_id alone is enough trace.
        details={"fleet_id": body.fleet_id},
        request=request,
    )
    await db.commit()

    # Always emit the canonical name the loader looks for in its default
    # search paths (%LOCALAPPDATA%\agora-softplayer\softplayer.env, next to
    # the .exe, or cwd). Operators can rename to disambiguate across
    # multiple fleets if they really need to, but the common path is
    # "drop the download where it belongs and start the player".
    filename = "softplayer.env"
    return Response(
        content=payload,
        media_type="text/plain; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            # Caching makes no sense for a secret-bearing download.
            "Cache-Control": "no-store",
        },
    )


@router.get("/provisioned-images", response_model=list[ProvisionedImageOut])
async def list_provisioned_images(
    user: User = Depends(require_permission(IMAGER_BUILD)),
    db: AsyncSession = Depends(get_db),
) -> list[ProvisionedImageOut]:
    """List built ``.img.xz`` artifacts (newest first, capped at 200).

    The UI surfaces this as the "Built images" section on the Imager
    tab so admins can re-download or clean up artifacts long after
    the build job poller has exited.  ``download_job_id`` is the FK
    the existing ``/api/imager/download/{job_id}`` redirect needs --
    it mints a fresh SAS on every hit so SAS lifetime is never an
    operator-visible concern.
    """
    rows = (
        await db.execute(
            select(ProvisionedImage)
            .order_by(ProvisionedImage.created_at.desc())
            .limit(200)
        )
    ).scalars().all()
    rows = list(rows)
    provisioning_ids = [
        pi.id for pi in rows if pi.status == ProvisionedImageStatus.PROVISIONING.value
    ]
    progress = await _latest_in_flight_progress(
        db, JobType.IMAGE_PROVISION, provisioning_ids
    )
    out: list[ProvisionedImageOut] = []
    for pi in rows:
        info = progress.get(pi.id)
        out.append(
            ProvisionedImageOut(
                id=pi.id,
                output_name=pi.output_name,
                fleet_id=pi.fleet_id,
                base_image_id=pi.base_image_id,
                base_variant=pi.base_variant,
                base_version=pi.base_version,
                status=pi.status,
                blob_path=pi.blob_path,
                output_size=pi.output_size,
                download_job_id=pi.provisioning_job_id,
                created_at=pi.created_at,
                built_at=pi.built_at,
                expires_at=pi.expires_at,
                progress_stage=(info[0] if info is not None else ""),
                progress_pct=(info[1] if info is not None else None),
            )
        )
    return out


@router.delete("/provisioned-images/{provisioned_image_id}", status_code=204)
async def delete_provisioned_image(
    provisioned_image_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(IMAGER_MANAGE)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> None:
    """Delete a built image (DB row + blob).

    Audited.  Idempotent against blob storage: a missing blob is a
    no-op (the lifecycle policy or a prior delete may have already
    cleaned it up).
    """
    pi = await db.get(ProvisionedImage, provisioned_image_id)
    if pi is None:
        raise HTTPException(status_code=404, detail="provisioned image not found")

    blob_path = pi.blob_path
    container = settings.provisioned_container

    await db.delete(pi)
    await audit_log(
        db, user=user, action="imager.provisioned_image.delete",
        resource_type="provisioned_image", resource_id=str(provisioned_image_id),
        details={
            "output_name": pi.output_name,
            "fleet_id": pi.fleet_id,
            "base_variant": pi.base_variant,
            "base_version": pi.base_version,
            "blob_path": blob_path,
        },
        request=request,
    )
    await db.commit()

    if blob_path:
        try:
            storage = get_storage()
            await storage.delete_blob(container, blob_path)
        except Exception:
            # Audit log already records the row delete; best-effort
            # blob cleanup is fine to swallow on failure.
            pass


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
        progress_stage=job.progress_stage or "",
        progress_pct=job.progress_pct,
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


class DownloadUrlOut(BaseModel):
    """Response body for :http:get:`/api/imager/download-url/{job_id}`."""

    url: str


async def _resolve_provisioned_sas(
    job_id: uuid.UUID,
    db: AsyncSession,
    settings: Settings,
) -> tuple[Job, ProvisionedImage, str]:
    """Validate an ``IMAGE_PROVISION`` job and mint a fresh SAS URL for it.

    Shared between :func:`download_provisioned` (302 redirect) and
    :func:`download_url_provisioned` (JSON for the "Copy download link"
    kebab action) so both code paths apply identical validation +
    blob-presence + SAS-minting rules.  Each caller is responsible
    for its own ``audit_log`` entry and ``db.commit``.

    Raises the same ``HTTPException``s the original endpoint did:

    * 404 — job is unknown / not ``IMAGE_PROVISION`` / no longer
      ``READY`` (e.g. the 24 h Azure lifecycle policy already
      expired the blob and the row is now ``EXPIRED``) / the blob is
      missing from storage.
    * 409 — job is known but has not reached ``DONE`` yet.
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
    return job, pi, url


def _provisioned_audit_details(job: Job, pi: ProvisionedImage) -> dict[str, Any]:
    """Identity fields stamped into download/copy-link audit rows."""
    return {
        "job_id": str(job.id),
        # base_image_id may be NULL if the cached base was deleted
        # after this build was produced (audit FK is SET NULL).
        # base_variant / base_version snapshot columns preserve
        # identity in that case.
        "base_image_id": str(pi.base_image_id) if pi.base_image_id else None,
        "base_variant": pi.base_variant,
        "base_version": pi.base_version,
        "fleet_id": pi.fleet_id,
        "output_name": pi.output_name,
    }


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
    job, pi, url = await _resolve_provisioned_sas(job_id, db, settings)

    await audit_log(
        db, user=user, action="imager.download",
        resource_type="provisioned_image", resource_id=str(pi.id),
        details=_provisioned_audit_details(job, pi),
        request=request,
    )
    await db.commit()

    return RedirectResponse(url=url, status_code=302)


@router.get("/download-url/{job_id}", response_model=DownloadUrlOut)
async def download_url_provisioned(
    job_id: uuid.UUID,
    request: Request,
    user: User = Depends(require_permission(IMAGER_BUILD)),
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> DownloadUrlOut:
    """Return a fresh SAS download URL for the provisioned image as JSON.

    Backs the built-images table's "Copy download link" kebab action
    so an operator can paste the URL into ``wget`` / ``curl`` from a
    Linux shell without going through the browser's redirect chain.

    Applies the same validation, blob-existence check, and SAS TTL
    as :func:`download_provisioned`.  Audited as ``imager.copy_link``
    so this is distinguishable from a normal browser download in the
    audit trail.
    """
    job, pi, url = await _resolve_provisioned_sas(job_id, db, settings)

    await audit_log(
        db, user=user, action="imager.copy_link",
        resource_type="provisioned_image", resource_id=str(pi.id),
        details=_provisioned_audit_details(job, pi),
        request=request,
    )
    await db.commit()

    return DownloadUrlOut(url=url)
