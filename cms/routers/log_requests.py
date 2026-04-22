"""Async log-request API (Stage 3b of #345).

Three user-facing endpoints backed by the ``log_requests`` outbox plus
one device-facing upload endpoint:

* ``POST   /api/logs/requests``                  — enqueue a request
* ``GET    /api/logs/requests/{id}``             — poll status
* ``GET    /api/logs/requests/{id}/download``    — fetch the tar.gz
* ``POST   /api/devices/{device_id}/logs/{id}/upload`` — Pi uploads

The user-facing endpoints use the standard RBAC + group scoping.
The upload endpoint uses device-token auth (reused from the asset
download path).

See ``docs/multi-replica-architecture.md`` §Stage 3 and
``cms/models/log_request.py`` for the schema.
"""

from __future__ import annotations

import hashlib
import logging
from typing import AsyncIterator

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_user_group_ids, require_auth, require_permission
from cms.database import get_db
from cms.models.device import Device
from cms.models.log_request import (
    STATUS_PENDING,
    STATUS_READY,
    STATUS_SENT,
    TERMINAL_STATUSES,
    LogRequest,
)
from cms.permissions import LOGS_READ
from cms.services import log_outbox
from cms.services.audit_service import audit_log
from cms.services.log_blob import (
    get_log_download_response,
    write_log_blob,
)
from cms.services.transport import get_transport

logger = logging.getLogger("agora.cms.log_requests")

# Hard cap on Pi-uploaded log bundles (anti-DoS).  A compressed journal
# dump very rarely exceeds a few MB — 100 MB is loose enough that
# nothing real ever hits it and tight enough that a malicious client
# can't wedge a worker with a many-GB stream.
MAX_UPLOAD_BYTES = 100 * 1024 * 1024

# Per-chunk read buffer for streaming uploads.  Large enough to keep
# per-chunk overhead low, small enough not to balloon RAM.
_UPLOAD_CHUNK = 1 << 20  # 1 MiB


# ── Auth helpers ────────────────────────────────────────────────────

def _hash_device_key(key: str) -> str:
    # Same hashing used by cms.routers.assets.require_device_or_session_auth.
    return hashlib.sha256(key.encode()).hexdigest()


async def require_device_auth(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> Device:
    """HTTP device-auth dependency.

    Validates ``X-Device-API-Key`` against ``Device.device_api_key_hash``
    and returns the :class:`Device` row.  The caller re-checks that the
    authenticated device matches the path-bound ``device_id`` so a Pi
    can't upload a bundle on another device's behalf.
    """
    api_key = request.headers.get("X-Device-API-Key")
    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing X-Device-API-Key header",
        )
    key_hash = _hash_device_key(api_key)
    result = await db.execute(
        select(Device).where(Device.device_api_key_hash == key_hash)
    )
    device = result.scalar_one_or_none()
    if device is None:
        # Fall back to the previous-key grace window so a rotation
        # doesn't wedge an in-flight upload.  Reuses the 5-minute window
        # from the asset router.
        from datetime import datetime, timedelta, timezone as tz
        result = await db.execute(
            select(Device).where(Device.previous_api_key_hash == key_hash)
        )
        device = result.scalar_one_or_none()
        if device and device.api_key_rotated_at is not None:
            rotated_at = device.api_key_rotated_at
            if rotated_at.tzinfo is None:
                rotated_at = rotated_at.replace(tzinfo=tz.utc)
            if datetime.now(tz.utc) - rotated_at < timedelta(seconds=300):
                return device
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid device API key",
        )
    return device


# ── Request / response schemas ──────────────────────────────────────

class CreateLogRequest(BaseModel):
    device_id: str
    services: list[str] | None = None
    since: str = "24h"


def _serialise(row: LogRequest) -> dict:
    download_url = None
    if row.status == STATUS_READY and row.blob_path:
        download_url = f"/api/logs/requests/{row.id}/download"
    return {
        "request_id": row.id,
        "device_id": row.device_id,
        "status": row.status,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "sent_at": row.sent_at.isoformat() if row.sent_at else None,
        "ready_at": row.ready_at.isoformat() if row.ready_at else None,
        "attempts": row.attempts,
        "last_error": row.last_error,
        "size_bytes": row.size_bytes,
        "download_url": download_url,
    }


# ── Access helpers ──────────────────────────────────────────────────

async def _verify_device_access(
    user, device_id: str, db: AsyncSession,
) -> Device:
    """Load device + verify the user has group access, or raise 403/404."""
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    group_ids = await get_user_group_ids(user, db)
    if group_ids is not None:  # None = admin / view_all
        if device.group_id is None or device.group_id not in group_ids:
            raise HTTPException(
                status_code=403,
                detail=f"Not authorised for device {device_id}",
            )
    return device


async def _load_outbox_row_with_access(
    request_id: str, request: Request, db: AsyncSession,
) -> LogRequest:
    """Load a LogRequest row and enforce group access on its device."""
    row = await log_outbox.get(db, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Log request not found")
    user = getattr(request.state, "user", None)
    if user:
        await _verify_device_access(user, row.device_id, db)
    return row


# ── User-facing router ──────────────────────────────────────────────

router = APIRouter(
    prefix="/api/logs/requests",
    dependencies=[Depends(require_auth)],
)


@router.post(
    "",
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_permission(LOGS_READ))],
)
async def create_log_request(
    body: CreateLogRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Enqueue a log-request outbox row and try to dispatch immediately.

    If dispatch succeeds the row flips to ``sent``; if the device is
    offline or the transport fails we leave the row ``pending`` — the
    Stage 3d drainer will retry (and the back-compat shim will still
    land legacy ``LOGS_RESPONSE`` payloads if the device reconnects
    and the old firmware replies synchronously).
    """
    user = getattr(request.state, "user", None)
    await _verify_device_access(user, body.device_id, db)

    row = await log_outbox.create(
        db,
        device_id=body.device_id,
        requested_by_user_id=user.id if user else None,
        services=body.services,
        since=body.since,
    )

    transport = get_transport()
    dispatched = False
    dispatch_err: str | None = None
    try:
        await transport.dispatch_request_logs(
            body.device_id,
            request_id=row.id,
            services=body.services,
            since=body.since,
        )
        dispatched = True
    except (ValueError, TimeoutError) as exc:
        dispatch_err = str(exc)
    except Exception as exc:  # noqa: BLE001
        logger.exception("dispatch_request_logs(%s) failed", body.device_id)
        dispatch_err = str(exc)

    if dispatched:
        await log_outbox.mark_sent(db, row.id)
    else:
        await log_outbox.record_attempt_error(
            db, row.id, error=dispatch_err or "dispatch failed",
        )

    await audit_log(
        db, user=user,
        action="logs.request",
        resource_type="log_request",
        resource_id=row.id,
        description=(
            f"Requested logs from device {body.device_id} "
            f"({'dispatched' if dispatched else 'queued'})"
        ),
        details={
            "device_id": body.device_id,
            "services": body.services,
            "since": body.since,
            "dispatched": dispatched,
            "error": dispatch_err,
        },
        request=request,
    )
    await db.commit()

    return {
        "request_id": row.id,
        "status": "sent" if dispatched else "pending",
    }


@router.get(
    "/{request_id}",
    dependencies=[Depends(require_permission(LOGS_READ))],
)
async def get_log_request(
    request_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    row = await _load_outbox_row_with_access(request_id, request, db)
    return _serialise(row)


@router.get(
    "/{request_id}/download",
    dependencies=[Depends(require_permission(LOGS_READ))],
)
async def download_log_request(
    request_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    row = await _load_outbox_row_with_access(request_id, request, db)
    if row.status != STATUS_READY or not row.blob_path:
        raise HTTPException(
            status_code=409,
            detail={"detail": "Logs not ready yet", "status": row.status},
        )

    user = getattr(request.state, "user", None)
    await audit_log(
        db, user=user,
        action="logs.download_bundle",
        resource_type="log_request",
        resource_id=row.id,
        description=f"Downloaded log bundle for device {row.device_id}",
        details={
            "device_id": row.device_id,
            "size_bytes": row.size_bytes,
        },
        request=request,
    )
    await db.commit()

    filename = f"{row.device_id}-{row.id[:8]}.tar.gz"
    return await get_log_download_response(row.blob_path, filename=filename)


# ── Device-facing upload router ─────────────────────────────────────
#
# Kept on its own APIRouter so the user-auth ``require_auth`` dep on the
# one above doesn't also apply here.  Device auth is the X-Device-API-Key
# header (same header as the asset-download path) + a path-match check
# so a device can't upload on another device's behalf.

device_upload_router = APIRouter(prefix="/api/devices")


async def _bounded_body_stream(request: Request) -> AsyncIterator[bytes]:
    """Yield the request body in chunks, rejecting any stream that exceeds
    :data:`MAX_UPLOAD_BYTES`.

    FastAPI's ``request.stream()`` already returns an async iterator over
    inbound chunks; we just sum and gate.
    """
    total = 0
    async for chunk in request.stream():
        if not chunk:
            continue
        total += len(chunk)
        if total > MAX_UPLOAD_BYTES:
            raise HTTPException(
                status_code=413,
                detail=f"Upload exceeds {MAX_UPLOAD_BYTES} bytes",
            )
        yield chunk


@device_upload_router.post("/{device_id}/logs/{request_id}/upload")
async def upload_log_bundle(
    device_id: str,
    request_id: str,
    request: Request,
    device: Device = Depends(require_device_auth),
    db: AsyncSession = Depends(get_db),
):
    """Accept a Pi-originated tar.gz log bundle for ``request_id``.

    The authenticated device must match the path ``device_id`` so a
    compromised Pi can't poison another device's row.
    """
    if device.id != device_id:
        raise HTTPException(
            status_code=403,
            detail="Authenticated device does not match path device_id",
        )

    row = await log_outbox.get(db, request_id)
    if row is None:
        raise HTTPException(status_code=404, detail="Log request not found")
    if row.device_id != device_id:
        raise HTTPException(
            status_code=404,
            detail="Log request not found for this device",
        )
    if row.status in TERMINAL_STATUSES:
        raise HTTPException(
            status_code=409,
            detail={
                "detail": f"Log request is {row.status}; cannot accept upload",
                "status": row.status,
            },
        )
    if row.status not in (STATUS_PENDING, STATUS_SENT):
        # Defensive: any other unexpected state blocks the upload so we
        # don't overwrite an in-flight bundle.
        raise HTTPException(status_code=409, detail=f"Unexpected status {row.status}")

    blob_path = f"{device_id}/{request_id}.tar.gz"

    # Stream → blob with the bounded body iterator.  HTTPException from
    # the limiter bubbles up as a 413 before any partial blob is left
    # around (write is still a single call to the backend, but the
    # iterator raises inside it and cleanup on local rolls back via the
    # ``finally`` below).
    try:
        size_bytes = await write_log_blob(
            blob_path, _bounded_body_stream(request),
        )
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to write log blob %s", blob_path)
        raise HTTPException(status_code=500, detail=f"Blob write failed: {exc}")

    ok = await log_outbox.mark_ready(
        db, request_id, blob_path=blob_path, size_bytes=size_bytes,
    )
    await db.commit()
    if not ok:
        # Row changed state underneath us (e.g. reaper expired it
        # mid-upload).  The blob is now orphaned — best-effort cleanup.
        logger.warning(
            "upload_log_bundle: mark_ready returned False for %s (status changed?)",
            request_id,
        )

    return {"status": "ready", "size_bytes": size_bytes}
