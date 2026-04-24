"""Business logic for the HTTPS device-bootstrap flow.

Thin service layer consumed by ``cms.routers.bootstrap`` — keeps the
router itself small, mechanical, and easy to read.  All crypto lives in
``cms.services.device_identity``; all ORM / transaction management
lives here.

See umbrella issue #420 for the bootstrap redesign context.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from cms.config import Settings
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_profile import DeviceProfile
from cms.models.pending_registration import PendingRegistration
from cms.services import device_identity


logger = logging.getLogger(__name__)


# ``pg_advisory_xact_lock`` key used to serialize cap-check + insert on
# ``POST /register``.  Any 63-bit constant works; this one is picked so
# it is trivially greppable in server-side pg_stat_activity.
_REGISTER_ADVISORY_LOCK_KEY = 0x420_A3_0001


class BootstrapCapReached(Exception):
    """Raised by :func:`register_device` when the pending-registrations
    hard cap has been reached.  Router translates to HTTP 503.
    """


class BootstrapAlreadyAdopted(Exception):
    """Raised by :func:`adopt_device` if the pairing secret maps to an
    already-adopted row or if the row was raced to adoption by another
    request between lookup and lock acquisition.
    """


class BootstrapPendingNotFound(Exception):
    """Raised by :func:`adopt_device` if the pairing secret doesn't
    match any pending row.
    """


class BootstrapPubkeyMismatch(Exception):
    """Raised by :func:`register_device` if a re-registration for the
    same ``pairing_secret_hash`` submits a different pubkey than the
    existing unadopted row.  Prevents an attacker (with fleet HMAC +
    pairing secret) from hijacking an in-flight registration by swapping
    their own pubkey in before adoption — the adopted payload would then
    be ECIES-encrypted to the attacker.  Router translates to HTTP 409.
    """


# ---------------------------------------------------------------------
# /register
# ---------------------------------------------------------------------


async def register_device(
    *,
    db: AsyncSession,
    device_id: str,
    pubkey_b64: str,
    pairing_secret_hash: str,
    metadata: dict[str, Any] | None,
    ip_address: str | None,
    settings: Settings,
) -> PendingRegistration:
    """Upsert a ``pending_registrations`` row.

    Serialises the cap-check + insert under a per-transaction advisory
    lock on PostgreSQL so that concurrent ``/register`` calls can't
    overshoot ``settings.pending_registrations_max``.  On SQLite the
    advisory lock is a no-op (safe — test concurrency is not a real
    threat).

    Raises :class:`BootstrapCapReached` if the cap is hit.
    """
    pubkey_b64 = device_identity.canonicalize_pubkey_b64(pubkey_b64)

    # Advisory lock is postgres-only; ignore the "function does not
    # exist" error on other backends (SQLite during tests).
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        await db.execute(
            text("SELECT pg_advisory_xact_lock(:k)"),
            {"k": _REGISTER_ADVISORY_LOCK_KEY},
        )

    # Re-registration path: same pairing_secret_hash → update in place.
    existing = (
        await db.execute(
            select(PendingRegistration).where(
                PendingRegistration.pairing_secret_hash == pairing_secret_hash,
            )
        )
    ).scalar_one_or_none()

    now = datetime.now(timezone.utc)

    if existing is not None:
        # If the row is already adopted we don't overwrite — re-registering
        # an already-adopted pairing hash is a spec violation (the device
        # should only re-use its secret while still in bootstrap mode).
        # Return the existing row as-is so the caller still gets a 202.
        if existing.adopted_at is not None:
            return existing
        # Security: require the pubkey to match the existing unadopted
        # row.  Allowing pubkey replacement here would let anyone with the
        # fleet HMAC secret AND the pairing secret swap in their own
        # pubkey before adoption, causing the admin-encrypted config
        # payload to be delivered to the attacker.  Legit "same device
        # retries" always re-uses the same keypair.  A factory-reset
        # device generates a new keypair AND a new pairing secret, so
        # it lands on the fresh-registration path below, not here.
        if existing.pubkey != pubkey_b64:
            raise BootstrapPubkeyMismatch(
                "pairing_secret_hash already in use by a different pubkey"
            )
        # Same-pubkey retry: refresh transport metadata only.  We do not
        # rewrite ``device_id`` either — it is keypair-stable and the
        # caller already proved possession of the keypair via fleet HMAC.
        existing.connection_metadata = metadata or None
        existing.ip_address = ip_address
        existing.updated_at = now
        await db.flush()
        return existing

    # Fresh registration — enforce the hard cap.
    cap = int(settings.pending_registrations_max)
    if cap > 0:
        count = (
            await db.execute(
                select(func.count()).select_from(PendingRegistration).where(
                    PendingRegistration.adopted_at.is_(None),
                )
            )
        ).scalar_one()
        if int(count) >= cap:
            raise BootstrapCapReached(
                f"pending_registrations cap reached ({count}/{cap})"
            )

    row = PendingRegistration(
        id=uuid.uuid4(),
        device_id=device_id,
        pubkey=pubkey_b64,
        pairing_secret_hash=pairing_secret_hash,
        connection_metadata=metadata or None,
        ip_address=ip_address,
        created_at=now,
        updated_at=now,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError:
        # Raced with another concurrent /register for the same pubkey.
        # Partial unique index on ``pubkey WHERE adopted_at IS NULL``
        # just tripped.  Roll back and return the row that won — keyed
        # on pubkey, not pairing_secret_hash, because the conflicting
        # request may have used the same keypair with a different
        # pairing secret (e.g. device factory-reset with fresh QR).
        await db.rollback()
        winner = (
            await db.execute(
                select(PendingRegistration).where(
                    PendingRegistration.pubkey == pubkey_b64,
                    PendingRegistration.adopted_at.is_(None),
                )
            )
        ).scalar_one_or_none()
        if winner is not None:
            return winner
        raise
    return row


# ---------------------------------------------------------------------
# /bootstrap-status
# ---------------------------------------------------------------------


async def get_bootstrap_status(
    *, db: AsyncSession, pubkey_b64: str,
) -> PendingRegistration | None:
    """Look up a pending_registrations row by pubkey.

    Returns the row (which may be pending or adopted), or ``None`` if
    no row exists for this pubkey.  Callers are responsible for
    interpreting ``adopted_at`` / ``outbox_ciphertext``.

    Also bumps ``polled_at`` on the first successful poll (used by the
    GC job to switch the row from the aggressive 1h TTL to the 24h
    TTL).
    """
    pubkey_b64 = device_identity.canonicalize_pubkey_b64(pubkey_b64)
    row = (
        await db.execute(
            select(PendingRegistration).where(
                PendingRegistration.pubkey == pubkey_b64,
            ).order_by(PendingRegistration.updated_at.desc())
        )
    ).scalars().first()
    if row is None:
        return None
    if row.polled_at is None:
        row.polled_at = datetime.now(timezone.utc)
        await db.flush()
    return row


# ---------------------------------------------------------------------
# pending_registrations GC (TTL reaper)
# ---------------------------------------------------------------------


async def reap_pending_registrations(
    *,
    db: AsyncSession,
    unpolled_ttl_seconds: int,
    polled_ttl_seconds: int,
    adopted_ttl_seconds: int,
) -> int:
    """Delete expired ``pending_registrations`` rows.

    Three TTLs, applied in descending order of aggressiveness:

    * ``unpolled_ttl_seconds`` — rows never polled by any device.  Most
      likely junk: HMAC-authed but the device either never came back
      online or is a bad actor burning cap slots.  Aggressive default
      (1h).
    * ``polled_ttl_seconds`` — rows a device polled but admin never
      adopted.  Legit pending registrations fall here; give admins
      generous time to notice the device in the UI (24h).
    * ``adopted_ttl_seconds`` — rows already adopted.  The device has
      polled the ciphertext (or we presume it has) and doesn't need the
      payload anymore; keep for a short window for troubleshooting then
      drop.  Zero or negative disables.

    Returns the total number of rows deleted.  Idempotent — safe to run
    on every replica, though the caller should gate behind a leader
    lock to avoid wasted DB work.
    """
    from sqlalchemy import delete, or_, and_

    now = datetime.now(timezone.utc)
    total = 0

    if unpolled_ttl_seconds > 0:
        cutoff = now - timedelta(seconds=unpolled_ttl_seconds)
        result = await db.execute(
            delete(PendingRegistration).where(
                PendingRegistration.adopted_at.is_(None),
                PendingRegistration.polled_at.is_(None),
                PendingRegistration.created_at < cutoff,
            )
        )
        total += result.rowcount or 0

    if polled_ttl_seconds > 0:
        cutoff = now - timedelta(seconds=polled_ttl_seconds)
        result = await db.execute(
            delete(PendingRegistration).where(
                PendingRegistration.adopted_at.is_(None),
                PendingRegistration.polled_at.is_not(None),
                PendingRegistration.polled_at < cutoff,
            )
        )
        total += result.rowcount or 0

    if adopted_ttl_seconds > 0:
        cutoff = now - timedelta(seconds=adopted_ttl_seconds)
        result = await db.execute(
            delete(PendingRegistration).where(
                PendingRegistration.adopted_at.is_not(None),
                PendingRegistration.adopted_at < cutoff,
            )
        )
        total += result.rowcount or 0

    if total > 0:
        await db.commit()
    else:
        await db.rollback()
    return total


# ---------------------------------------------------------------------
# /adopt
# ---------------------------------------------------------------------


def _build_bootstrap_payload(
    *, device_row_id: str, wps_url: str, wps_jwt: str,
    jwt_lifetime_minutes: int,
) -> bytes:
    expires_at = (
        datetime.now(timezone.utc) + timedelta(minutes=jwt_lifetime_minutes)
    ).isoformat().replace("+00:00", "Z")
    payload = {
        "device_id": device_row_id,
        "wps_jwt": wps_jwt,
        "wps_url": wps_url,
        "jwt_expires_at": expires_at,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


async def _lock_pending_for_adoption(
    db: AsyncSession, pairing_secret_hash: str,
) -> PendingRegistration | None:
    """``SELECT ... FOR UPDATE`` on PostgreSQL; plain SELECT on SQLite.

    Ensures two concurrent /adopt calls for the same pairing secret
    can't both pass the "not adopted" check.
    """
    stmt = select(PendingRegistration).where(
        PendingRegistration.pairing_secret_hash == pairing_secret_hash,
    )
    dialect = db.bind.dialect.name if db.bind is not None else ""
    if dialect == "postgresql":
        stmt = stmt.with_for_update()
    return (await db.execute(stmt)).scalar_one_or_none()


async def adopt_device(
    *,
    db: AsyncSession,
    pairing_secret: str,
    profile_id: str,
    name: str | None,
    location: str | None,
    group_id: str | None,
    mint_wps_jwt,  # async callable (device_id) -> dict(url=..., token=...)
    settings: Settings,
) -> tuple[Device, PendingRegistration]:
    """Adopt a pending_registrations row.

    Atomic: looks up (FOR UPDATE) the pending row by pairing-secret hash,
    creates the ``devices`` row, mints a WPS JWT, ECIES-encrypts the
    bootstrap payload to the device's pubkey, and writes everything in
    a single transaction.  Caller is responsible for committing.
    """
    pairing_secret_hash = device_identity.sha256_hex(
        pairing_secret.encode("utf-8"),
    )
    pending = await _lock_pending_for_adoption(db, pairing_secret_hash)
    if pending is None:
        raise BootstrapPendingNotFound(pairing_secret_hash)
    if pending.adopted_at is not None:
        raise BootstrapAlreadyAdopted(pairing_secret_hash)

    # Validate optional group_id.
    if group_id is not None:
        try:
            group_uuid = uuid.UUID(group_id)
        except (ValueError, AttributeError) as e:
            raise ValueError("group_not_found") from e
        grp = (
            await db.execute(
                select(DeviceGroup).where(DeviceGroup.id == group_uuid)
            )
        ).scalar_one_or_none()
        if grp is None:
            raise ValueError("group_not_found")

    # Validate required profile_id.
    try:
        profile_uuid = uuid.UUID(profile_id)
    except (ValueError, AttributeError) as e:
        raise ValueError("profile_not_found") from e
    prof = (
        await db.execute(
            select(DeviceProfile).where(DeviceProfile.id == profile_uuid)
        )
    ).scalar_one_or_none()
    if prof is None:
        raise ValueError("profile_not_found")

    # Create the devices row.  ID is a fresh UUID — the device doesn't
    # pick its own id in the new flow; it learns it from the outbox
    # payload.  Any device_id the device reported to /register was
    # advisory metadata only.
    device_row_id = str(uuid.uuid4())
    device = Device(
        id=device_row_id,
        name=name or "",
        location=location or "",
        status=DeviceStatus.ADOPTED,
        group_id=uuid.UUID(group_id) if group_id else None,
        profile_id=uuid.UUID(profile_id),
        pubkey=pending.pubkey,
    )
    db.add(device)
    try:
        await db.flush()
    except IntegrityError as e:
        # Concurrent /adopt collision or pubkey already present on
        # another devices row (shouldn't happen because pending's
        # partial unique index on pubkey blocks concurrent pending
        # rows, but double-check).
        raise BootstrapAlreadyAdopted(pairing_secret_hash) from e

    # Mint a fresh WPS JWT for the new device.
    access = await mint_wps_jwt(device_row_id)
    wps_url = access.get("url") or access.get("baseUrl") or ""
    wps_token = access.get("token") or access.get("accessToken") or ""
    if not wps_url or not wps_token:
        # Surface as a 500 in the router; the caller's exception
        # handler rolls back the transaction so we don't end up with
        # a half-adopted row.
        raise RuntimeError("failed to mint WPS access token")

    plaintext = _build_bootstrap_payload(
        device_row_id=device_row_id,
        wps_url=wps_url,
        wps_jwt=wps_token,
        jwt_lifetime_minutes=settings.bootstrap_wps_jwt_minutes,
    )
    ciphertext_b64 = device_identity.encrypt_for_device(
        pending.pubkey, plaintext,
    )

    now = datetime.now(timezone.utc)
    pending.adopted_at = now
    pending.outbox_ciphertext = ciphertext_b64
    pending.adopted_device_id = device_row_id
    pending.updated_at = now
    await db.flush()

    return device, pending
