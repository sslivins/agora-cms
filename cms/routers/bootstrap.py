"""HTTPS bootstrap + connect-token endpoints (issue #420 stage A.3).

Four endpoints handle the full device-side lifecycle that used to run
over the unauthenticated ``/ws/cms`` WebSocket:

* ``POST /api/devices/register`` — anonymous, gated by fleet HMAC.
  Device publishes its pubkey + hash(pairing_secret) + advisory
  metadata.  Returns 202 {"status": "pending"} always so probing
  can't distinguish valid from invalid fleet MACs.
* ``GET  /api/devices/bootstrap-status`` — anonymous, polled by the
  device every few seconds.  Returns ``pending`` until the operator
  scans the QR code and adopts, then returns the ECIES-encrypted
  bootstrap payload (device_id + WPS URL + WPS JWT).
* ``POST /api/devices/adopt`` — CMS session auth (DEVICES_MANAGE).
  Consumes the pairing secret, creates the devices row, mints the
  initial WPS JWT, writes the encrypted payload into
  ``pending_registrations.outbox_ciphertext``.  Coexists with the
  legacy ``/api/devices/{device_id}/adopt`` endpoint — the two have
  different paths so they never collide.
* ``POST /api/devices/connect-token`` — anonymous + signed.  Device
  proves control of its pubkey via an ed25519 signature and gets a
  fresh WPS JWT.  Replaces the ``/api/devices/{id}/client-token``
  path that relied on the legacy device API-key header.

Rate limiting is implemented per-endpoint via a small token-bucket in
``cms.services.rate_limit`` rather than slowapi to avoid adding a new
top-level dependency.  See ``_ip_ratelimited`` below.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import logging
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Deque, Tuple

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_settings, require_permission
from cms.config import Settings
from cms.database import get_db
from cms.permissions import DEVICES_MANAGE
from cms.schemas.bootstrap import (
    AdoptPendingRequest,
    BootstrapAdoptRequest,
    BootstrapAdoptResponse,
    BootstrapStatusResponse,
    ConnectTokenRequest,
    ConnectTokenResponse,
    PendingDeviceSummary,
    PendingDevicesResponse,
    RegisterRequest,
    RegisterResponse,
)
from cms.services import device_bootstrap, device_identity
from cms.services.audit_service import audit_log
from cms.services.transport import get_transport


logger = logging.getLogger(__name__)


# Dedicated router.  No blanket ``require_auth`` dep — per-endpoint
# authentication is wildly different (anonymous + HMAC, anonymous +
# signature, session + permission).  DO NOT merge with the main
# ``devices`` router.
router = APIRouter(prefix="/api/devices", tags=["bootstrap"])


# ---------------------------------------------------------------------
# In-memory IP rate limiter.  Each bucket is a deque of timestamps; on
# request we drop timestamps older than the window and reject if the
# remaining count is at or above the per-window limit.  Per-replica
# under N>1, which is fine for bot deterrence — the real defence is
# the fleet HMAC and the 50K pending-registrations cap.
# ---------------------------------------------------------------------


_buckets: dict[Tuple[str, str], Deque[float]] = defaultdict(deque)


def _client_ip(request: Request) -> str | None:
    """Return the best-effort client IP for a request.

    Prefers the first entry in X-Forwarded-For when present so we record
    the real device IP instead of the Container Apps envoy ingress hop.
    Falls back to ``request.client.host``.  Mirrors the same logic used
    by ``audit_service._resolve_ip`` — kept local here to avoid a
    cross-module import from the router layer.
    """
    xff = request.headers.get("x-forwarded-for") if hasattr(request, "headers") else None
    if xff:
        first = xff.split(",")[0].strip()
        if first:
            return first
    if request.client:
        return request.client.host
    return None


def _ip_ratelimited(
    request: Request, *, key: str, limit: int, window_sec: int,
) -> None:
    ip = _client_ip(request) or "unknown"
    bucket_key = (key, ip)
    now = time.monotonic()
    bucket = _buckets[bucket_key]
    cutoff = now - window_sec
    while bucket and bucket[0] < cutoff:
        bucket.popleft()
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="rate_limited")
    bucket.append(now)


# ---------------------------------------------------------------------
# Fleet HMAC verification for POST /register
# ---------------------------------------------------------------------


async def _verify_fleet_hmac(
    request: Request, body: RegisterRequest, settings: Settings,
) -> None:
    """Validate the fleet HMAC on an incoming /register request.

    Every /register must carry ``X-Fleet-Id``, ``X-Fleet-Timestamp``
    (unix seconds, ±300s skew window), ``X-Fleet-Nonce`` (hex), and
    ``X-Fleet-Mac`` (base64 HMAC-SHA256).  The canonical input is
    defined by :func:`device_identity.fleet_hmac_input`.

    Fleet secrets are configured via ``FLEET_REGISTER_SECRETS`` (JSON
    map of fleet_id → base64 secret).  An empty map means every
    /register is rejected, which is the intended secure-by-default.
    Nonces are persisted in the in-memory nonce cache to prevent replay
    for the duration of :data:`Settings.bootstrap_nonce_ttl_seconds`.
    """
    fleet_id = request.headers.get("x-fleet-id") or ""
    ts_raw = request.headers.get("x-fleet-timestamp") or ""
    nonce = request.headers.get("x-fleet-nonce") or ""
    mac_b64 = request.headers.get("x-fleet-mac") or ""
    if not fleet_id or not ts_raw or not nonce or not mac_b64:
        raise HTTPException(status_code=401, detail="fleet_hmac_missing")

    try:
        ts = int(ts_raw)
    except ValueError as e:
        raise HTTPException(status_code=401, detail="fleet_hmac_bad_timestamp") from e

    if not device_identity.timestamp_within_skew(ts, 300):
        raise HTTPException(status_code=401, detail="fleet_hmac_stale")

    secret_b64 = (settings.fleet_register_secrets or {}).get(fleet_id)
    if not secret_b64:
        # Secure-by-default: no secret configured for this fleet ID, no
        # access.  Distinguishable-vs-valid-HMAC is fine here: we return
        # 401, the legitimate device returns 202.  Not returning extra
        # detail preserves the non-discoverable property.
        raise HTTPException(status_code=401, detail="fleet_hmac_bad")

    try:
        secret = base64.b64decode(secret_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        logger.error("FLEET_REGISTER_SECRETS[%s] is not valid base64", fleet_id)
        raise HTTPException(status_code=500, detail="fleet_secret_misconfigured") from e

    canonical = device_identity.fleet_hmac_input(
        device_id=body.device_id,
        pubkey=body.pubkey,
        pairing_secret_hash=body.pairing_secret_hash,
        fleet_id=fleet_id,
        timestamp=str(ts),
        nonce=nonce,
    )
    if not device_identity.verify_fleet_hmac(secret, canonical, mac_b64):
        raise HTTPException(status_code=401, detail="fleet_hmac_bad")

    # Replay protection — atomic check-and-record.
    nonce_cache = _nonce_cache(request)
    if not await nonce_cache.check_and_record("fleet", nonce):
        raise HTTPException(status_code=401, detail="fleet_hmac_replay")


def _nonce_cache(request: Request) -> device_identity.NonceCache:
    """Lazily return the app-wide nonce cache.

    Lives on ``app.state.bootstrap_nonce_cache`` — populated by the
    lifespan handler in prod and by a fixture in the test suite.
    If neither has initialised it (e.g. a minimal test that bypasses
    ``lifespan``), create a fallback on demand so we don't NPE.
    """
    cache = getattr(request.app.state, "bootstrap_nonce_cache", None)
    if cache is None:
        settings = get_settings()
        cache = device_identity.InMemoryNonceCache(
            ttl_seconds=settings.bootstrap_nonce_ttl_seconds,
        )
        request.app.state.bootstrap_nonce_cache = cache
    return cache


# ---------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------


@router.post("/register", response_model=RegisterResponse, status_code=202)
async def register(
    body: RegisterRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> RegisterResponse:
    """Device-initiated registration.  Anonymous but fleet-HMAC gated."""
    _ip_ratelimited(request, key="register", limit=10, window_sec=60)

    await _verify_fleet_hmac(request, body, settings)

    # Canonicalise + validate the pubkey early so malformed input fails
    # before we touch the database.  ``register_device`` also re-normalises
    # for belt-and-braces.
    try:
        device_identity.canonicalize_pubkey_b64(body.pubkey)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    client_ip = _client_ip(request)
    try:
        await device_bootstrap.register_device(
            db=db,
            device_id=body.device_id,
            pubkey_b64=body.pubkey,
            pairing_secret_hash=body.pairing_secret_hash.lower(),
            metadata=body.metadata,
            ip_address=client_ip,
            settings=settings,
        )
        await db.commit()
    except device_bootstrap.BootstrapCapReached as e:
        await db.rollback()
        logger.warning("/register rejected: %s", e)
        raise HTTPException(status_code=503, detail="registration_capacity_exceeded") from e
    except device_bootstrap.BootstrapPubkeyMismatch as e:
        await db.rollback()
        logger.warning("/register rejected: %s", e)
        raise HTTPException(status_code=409, detail="pubkey_mismatch") from e
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        logger.exception("/register internal error")
        raise HTTPException(status_code=500, detail="internal_error")

    return RegisterResponse(status="pending")


@router.get("/bootstrap-status", response_model=BootstrapStatusResponse)
async def bootstrap_status(
    pubkey: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> BootstrapStatusResponse:
    """Poll endpoint the device hits until the operator adopts it."""
    _ip_ratelimited(request, key="bootstrap-status", limit=120, window_sec=60)

    try:
        normalised = device_identity.canonicalize_pubkey_b64(pubkey)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    row = await device_bootstrap.get_bootstrap_status(
        db=db, pubkey_b64=normalised,
    )
    if row is None:
        # No row at all for this pubkey.  Use 404 rather than 200
        # "pending" so a device that's been factory-reset (new pubkey)
        # knows it needs to POST /register again.
        raise HTTPException(status_code=404, detail="not_found")
    await db.commit()
    if row.adopted_at is None or row.outbox_ciphertext is None:
        return BootstrapStatusResponse(status="pending", payload=None)
    return BootstrapStatusResponse(
        status="adopted", payload=row.outbox_ciphertext,
    )


@router.post(
    "/adopt",
    response_model=BootstrapAdoptResponse,
    dependencies=[Depends(require_permission(DEVICES_MANAGE))],
)
async def adopt(
    body: BootstrapAdoptRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BootstrapAdoptResponse:
    """Operator-initiated adoption from the CMS UI (QR scanner).

    Coexists with the legacy ``/api/devices/{device_id}/adopt`` — that
    one handles devices that completed the old WS-register flow; this
    one handles devices that came in via the HTTPS bootstrap flow.
    """
    _ip_ratelimited(request, key="adopt", limit=60, window_sec=60)
    # Per-admin-user bucket (60/min) on top of per-IP — prevents a
    # single admin session from evading the limit by spreading across
    # NATed IPs, and protects shared-NAT admins from throttling each
    # other.  User is populated by ``require_permission`` above.
    _user = getattr(request.state, "user", None)
    _user_id = getattr(_user, "id", None) if _user is not None else None
    if _user_id is not None:
        now = time.monotonic()
        bucket = _buckets[("adopt-user", str(_user_id))]
        cutoff = now - 60
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= 60:
            raise HTTPException(status_code=429, detail="rate_limited")
        bucket.append(now)

    async def _mint(device_row_id: str) -> dict[str, Any]:
        transport = get_transport()
        if not hasattr(transport, "get_client_access_token"):
            raise HTTPException(
                status_code=500,
                detail="Transport does not support client tokens",
            )
        return await transport.get_client_access_token(
            device_row_id,
            minutes_to_expire=settings.bootstrap_wps_jwt_minutes,
        )

    try:
        device, pending = await device_bootstrap.adopt_device(
            db=db,
            pairing_secret=body.pairing_secret,
            profile_id=body.profile_id,
            name=body.name,
            location=body.location,
            group_id=body.group_id,
            mint_wps_jwt=_mint,
            settings=settings,
        )
    except device_bootstrap.BootstrapPendingNotFound:
        await db.rollback()
        raise HTTPException(status_code=404, detail="pending_not_found")
    except device_bootstrap.BootstrapAlreadyAdopted:
        await db.rollback()
        raise HTTPException(status_code=409, detail="already_adopted")
    except ValueError as e:
        await db.rollback()
        msg = str(e)
        if msg in ("group_not_found", "profile_not_found"):
            raise HTTPException(status_code=404, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        logger.exception("/adopt internal error")
        raise HTTPException(status_code=500, detail="internal_error")

    await audit_log(
        db,
        user=getattr(request.state, "user", None),
        action="device.adopt",
        resource_type="device",
        resource_id=str(device.id),
        description=f"Adopted device '{device.name or device.id}' via bootstrap",
        details={
            "name": device.name,
            "location": device.location,
            "group_id": str(device.group_id) if device.group_id else None,
            "profile_id": str(device.profile_id) if device.profile_id else None,
            "pending_id": str(pending.id),
        },
        request=request,
    )
    await db.commit()

    return BootstrapAdoptResponse(device_id=device.id, status="adopted")


# ---------------------------------------------------------------------
# /pending + /adopt-pending (UI-driven adopt-by-row-id flow)
# ---------------------------------------------------------------------


def _pending_to_summary(row) -> PendingDeviceSummary:
    metadata = dict(row.connection_metadata or {})
    return PendingDeviceSummary(
        id=str(row.id),
        device_id=row.device_id,
        pubkey=row.pubkey,
        metadata=metadata,
        ip_address=row.ip_address,
        created_at=(
            row.created_at.isoformat().replace("+00:00", "Z")
            if row.created_at is not None else ""
        ),
        polled_at=(
            row.polled_at.isoformat().replace("+00:00", "Z")
            if row.polled_at is not None else None
        ),
        has_polled=row.polled_at is not None,
    )


@router.get(
    "/pending",
    response_model=PendingDevicesResponse,
    dependencies=[Depends(require_permission(DEVICES_MANAGE))],
)
async def list_pending(
    db: AsyncSession = Depends(get_db),
) -> PendingDevicesResponse:
    """List every pending (un-adopted) device registration.

    Drives the "Pending Devices" section of the devices page.  Items
    are sorted newest-first so the most recently connected device is
    always on top.
    """
    rows = await device_bootstrap.list_pending_registrations(db)
    return PendingDevicesResponse(
        items=[_pending_to_summary(r) for r in rows],
    )


@router.post(
    "/adopt-pending",
    response_model=BootstrapAdoptResponse,
    dependencies=[Depends(require_permission(DEVICES_MANAGE))],
)
async def adopt_pending(
    body: AdoptPendingRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> BootstrapAdoptResponse:
    """Adopt a pending row by its id.

    Same effect as ``POST /adopt`` with the pairing secret, but the
    admin never sees the secret — it stays on the wire between the
    device and CMS as an idempotency key.
    """
    _ip_ratelimited(request, key="adopt", limit=60, window_sec=60)
    _user = getattr(request.state, "user", None)
    _user_id = getattr(_user, "id", None) if _user is not None else None
    if _user_id is not None:
        now = time.monotonic()
        bucket = _buckets[("adopt-user", str(_user_id))]
        cutoff = now - 60
        while bucket and bucket[0] < cutoff:
            bucket.popleft()
        if len(bucket) >= 60:
            raise HTTPException(status_code=429, detail="rate_limited")
        bucket.append(now)

    try:
        pending_uuid = uuid.UUID(body.pending_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="pending_not_found")

    async def _mint(device_row_id: str) -> dict[str, Any]:
        transport = get_transport()
        if not hasattr(transport, "get_client_access_token"):
            raise HTTPException(
                status_code=500,
                detail="Transport does not support client tokens",
            )
        return await transport.get_client_access_token(
            device_row_id,
            minutes_to_expire=settings.bootstrap_wps_jwt_minutes,
        )

    try:
        device, pending = await device_bootstrap.adopt_pending_by_id(
            db=db,
            pending_id=pending_uuid,
            profile_id=body.profile_id,
            name=body.name,
            location=body.location,
            group_id=body.group_id,
            mint_wps_jwt=_mint,
            settings=settings,
        )
    except device_bootstrap.BootstrapPendingNotFound:
        await db.rollback()
        raise HTTPException(status_code=404, detail="pending_not_found")
    except device_bootstrap.BootstrapAlreadyAdopted:
        await db.rollback()
        raise HTTPException(status_code=409, detail="already_adopted")
    except ValueError as e:
        await db.rollback()
        msg = str(e)
        if msg in ("group_not_found", "profile_not_found"):
            raise HTTPException(status_code=404, detail=msg) from e
        raise HTTPException(status_code=400, detail=msg) from e
    except HTTPException:
        await db.rollback()
        raise
    except Exception:
        await db.rollback()
        logger.exception("/adopt-pending internal error")
        raise HTTPException(status_code=500, detail="internal_error")

    await audit_log(
        db,
        user=getattr(request.state, "user", None),
        action="device.adopt",
        resource_type="device",
        resource_id=str(device.id),
        description=(
            f"Adopted device '{device.name or device.id}' via pending list"
        ),
        details={
            "name": device.name,
            "location": device.location,
            "group_id": str(device.group_id) if device.group_id else None,
            "profile_id": str(device.profile_id) if device.profile_id else None,
            "pending_id": str(pending.id),
            "flow": "adopt-pending",
        },
        request=request,
    )
    await db.commit()

    return BootstrapAdoptResponse(device_id=device.id, status="adopted")


@router.delete(
    "/pending/{pending_id}",
    status_code=204,
    dependencies=[Depends(require_permission(DEVICES_MANAGE))],
)
async def reject_pending(
    pending_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> None:
    """Reject / drop an un-adopted pending row.

    Useful for cleaning up bogus or duplicate registrations from the
    UI without waiting for the reaper's TTL.  Refuses to delete rows
    that have already been adopted.
    """
    try:
        pending_uuid = uuid.UUID(pending_id)
    except (ValueError, AttributeError):
        raise HTTPException(status_code=404, detail="pending_not_found")

    try:
        deleted = await device_bootstrap.delete_pending(db, pending_uuid)
    except Exception:
        await db.rollback()
        logger.exception("/pending delete internal error")
        raise HTTPException(status_code=500, detail="internal_error")

    if not deleted:
        await db.rollback()
        raise HTTPException(status_code=404, detail="pending_not_found")

    await audit_log(
        db,
        user=getattr(request.state, "user", None),
        action="device.pending.reject",
        resource_type="pending_registration",
        resource_id=str(pending_uuid),
        description="Rejected pending device registration from UI",
        details={"pending_id": str(pending_uuid)},
        request=request,
    )
    await db.commit()
    return None


@router.post("/connect-token", response_model=ConnectTokenResponse)
async def connect_token(
    body: ConnectTokenRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> ConnectTokenResponse:
    """Mint a fresh WPS JWT for an adopted device.

    Proof-of-possession of the device's ed25519 private key via a
    signed ``device_id|timestamp|nonce`` canonical string.  Replaces
    the legacy ``GET /api/devices/{id}/client-token`` endpoint that
    relied on the ``X-Device-API-Key`` header.
    """
    _ip_ratelimited(request, key="connect-token", limit=120, window_sec=60)

    from sqlalchemy import select as _select
    from cms.models.device import Device, DeviceStatus

    device = (
        await db.execute(
            _select(Device).where(Device.id == body.device_id)
        )
    ).scalar_one_or_none()
    if device is None or not device.pubkey or device.status != DeviceStatus.ADOPTED:
        # Uniform 401 for "no device", "device exists but no pubkey
        # (revoked)", "device not in adopted state (pending/removed)",
        # and "signature fails".  Don't leak which.
        raise HTTPException(status_code=401, detail="unauthorized")

    if not device_identity.timestamp_within_skew(body.timestamp, 60):
        raise HTTPException(status_code=401, detail="unauthorized")

    message = device_identity.connect_token_canonical_bytes(
        body.device_id, str(body.timestamp), body.nonce,
    )
    if not device_identity.verify_ed25519_signature(
        device.pubkey, message, body.signature,
    ):
        raise HTTPException(status_code=401, detail="unauthorized")

    nonce_cache = _nonce_cache(request)
    if not await nonce_cache.check_and_record("connect-token", body.nonce):
        raise HTTPException(status_code=401, detail="unauthorized")

    transport = get_transport()
    if not hasattr(transport, "get_client_access_token"):
        raise HTTPException(
            status_code=500,
            detail="Transport does not support client tokens",
        )
    minutes = settings.bootstrap_wps_jwt_minutes
    token = await transport.get_client_access_token(
        body.device_id, minutes_to_expire=minutes,
    )
    url = token.get("url") or token.get("baseUrl") or ""
    jwt = token.get("token") or token.get("accessToken") or ""
    if not url or not jwt:
        raise HTTPException(status_code=500, detail="token_mint_failed")

    from datetime import timedelta as _td
    expires_at = (
        datetime.now(timezone.utc) + _td(minutes=minutes)
    ).isoformat().replace("+00:00", "Z")

    return ConnectTokenResponse(wps_jwt=jwt, wps_url=url, expires_at=expires_at)
