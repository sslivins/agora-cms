"""Fleet registry service — DB-backed read/write for the fleets table.

Replaces the env-only ``Settings.fleet_register_secrets`` map. All
fleet-related read paths (HMAC verify, imager Build dropdown,
``GET /api/imager/fleets``) and write paths (CRUD endpoints) go
through this module.

## Locking convention (multi-replica safety)

Two operations can race on the same fleet row across replicas:

1. ``delete_fleet`` (admin clicks Delete in UI / API)
2. A concurrent ``POST /api/imager/build`` referencing the same fleet

Without locking, the build can read the fleet, the delete commits,
the build inserts a ``provisioned_images`` row whose ``fleet_id``
column references a now-soft-deleted fleet — the resulting image
would carry HMAC material the operator just revoked.

Convention enforced here:

* ``delete_fleet`` takes ``SELECT ... FOR UPDATE`` on the fleet row
  inside the transaction that flips ``deleted_at``.
* ``get_fleet_for_build`` (called by the build endpoint) takes
  ``SELECT ... FOR SHARE`` on the fleet row in the same transaction
  that inserts the ``provisioned_images`` row.

Postgres serialises the two — either the build sees the row as
deleted (returns 404) or the delete blocks until the build commits.
The HMAC-verify path (``get_fleet_secret``) deliberately does NOT
take a lock — register requests are high-frequency and a small
race window between "admin starts deleting" and "Pi finishes
register" is acceptable (the Pi gets a 202; the next time it
re-registers after the delete commits, it will fail, which is
correct).

## No in-memory cache

Read volume is low (Pi register is first-boot only; imager Build
is a one-off operator click). Postgres SELECT-by-fleet-id with
the partial unique index is < 1ms. Caching would introduce
invalidation pain across replicas (PG NOTIFY or TTL-based
staleness) for negligible gain.
"""

from __future__ import annotations

import base64
import binascii
import logging
import secrets as _secrets
import uuid
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from shared.models.fleet import Fleet


logger = logging.getLogger(__name__)


class FleetRegistryError(Exception):
    """Base for registry-specific errors."""


class FleetSecretMisconfigured(FleetRegistryError):
    """Stored ``secret_b64`` failed to decode as base64.

    Typically a hand-edited row or a botched migration. The
    HMAC-verify path raises this so the caller can return 500
    rather than silently failing closed (which would look like a
    legitimate-but-unauthorized device to the operator)."""


class FleetAlreadyExists(FleetRegistryError):
    """Active fleet with the given ``fleet_id`` already exists.

    Soft-deleted rows do NOT block a new active row of the same
    name (partial unique index ``deleted_at IS NULL``)."""


# ---------------------------------------------------------------------
# Read paths
# ---------------------------------------------------------------------


async def get_fleet_secret(db: AsyncSession, fleet_id: str) -> bytes | None:
    """Return the raw HMAC secret bytes for ``fleet_id``, or ``None``.

    ``None`` means "no active fleet by that id" — the HMAC-verify
    caller surfaces this as a 401 (secure-by-default).

    Raises :class:`FleetSecretMisconfigured` if the row exists but
    its ``secret_b64`` is not valid base64.
    """
    row = await _get_active_fleet(db, fleet_id)
    if row is None:
        return None
    try:
        return base64.b64decode(row.secret_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        logger.error("fleets.secret_b64 for %r is not valid base64", fleet_id)
        raise FleetSecretMisconfigured(fleet_id) from exc


async def list_active_fleets(db: AsyncSession) -> list[Fleet]:
    """Return all non-deleted fleet rows, ordered by ``fleet_id``."""
    stmt = (
        select(Fleet)
        .where(Fleet.deleted_at.is_(None))
        .order_by(Fleet.fleet_id)
    )
    return list((await db.execute(stmt)).scalars().all())


async def get_fleet_for_build(
    db: AsyncSession, fleet_id: str,
) -> Fleet | None:
    """Return the fleet row with a ``FOR SHARE`` lock, or ``None``.

    Build callers MUST be inside a transaction with ``provisioned_images``
    insert in the same unit-of-work — the lock prevents a concurrent
    delete from removing the fleet between read and insert. See module
    docstring.
    """
    stmt = (
        select(Fleet)
        .where(Fleet.fleet_id == fleet_id, Fleet.deleted_at.is_(None))
        .with_for_update(read=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


# ---------------------------------------------------------------------
# Write paths
# ---------------------------------------------------------------------


async def create_fleet(
    db: AsyncSession,
    *,
    fleet_id: str,
    description: str | None = None,
    created_by: uuid.UUID | None = None,
    secret_b64: str | None = None,
) -> Fleet:
    """Insert a new fleet row.

    By default a fresh 32-byte secret is generated and base64-encoded.
    ``secret_b64`` may be passed to support the env→DB migration path
    where the operator wants to preserve the existing HMAC material
    so already-flashed Pis keep working — pass-through is otherwise
    discouraged.

    Raises :class:`FleetAlreadyExists` if an active row with the same
    ``fleet_id`` exists.
    """
    if secret_b64 is None:
        secret_b64 = base64.b64encode(_secrets.token_bytes(32)).decode("ascii")
    else:
        # Validate caller-supplied material early — better to 422 here
        # than to discover at first /register that the row is broken.
        try:
            base64.b64decode(secret_b64, validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError("secret_b64 is not valid base64") from exc

    row = Fleet(
        fleet_id=fleet_id,
        secret_b64=secret_b64,
        description=description,
        created_by=created_by,
    )
    db.add(row)
    try:
        await db.flush()
    except IntegrityError as exc:
        await db.rollback()
        raise FleetAlreadyExists(fleet_id) from exc
    return row


async def delete_fleet(db: AsyncSession, fleet_id: str) -> bool:
    """Soft-delete the active fleet with the given id.

    Returns ``True`` if a row was deleted, ``False`` if no active
    row matched (idempotent).

    Takes ``SELECT FOR UPDATE`` to serialise against concurrent
    builds — see module docstring.
    """
    stmt = (
        select(Fleet)
        .where(Fleet.fleet_id == fleet_id, Fleet.deleted_at.is_(None))
        .with_for_update()
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        return False
    row.deleted_at = datetime.now(timezone.utc)
    await db.flush()
    return True


# ---------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------


async def _get_active_fleet(
    db: AsyncSession, fleet_id: str,
) -> Fleet | None:
    stmt = select(Fleet).where(
        Fleet.fleet_id == fleet_id, Fleet.deleted_at.is_(None)
    )
    return (await db.execute(stmt)).scalar_one_or_none()
