"""Shared device registration logic.

The direct-WebSocket endpoint (``cms/routers/ws.py``) and the Azure Web
PubSub upstream webhook (``cms/routers/wps_webhook.py``) both receive a
``register`` message as the first thing a device says over the wire.
Most of the bookkeeping — metadata refresh, auth-token handling, profile
auto-assign — is identical in both transports, so it lives here.

The only case that stays transport-specific is the brand-new-device
bootstrap: that path touches the raw WebSocket in ``ws.py`` to push an
``AuthAssignedMessage`` back over the wire before the normal message
loop starts.  Over WPS, brand-new devices can't even reach
``connect-token`` (which requires a device row + API key), so WPS
traffic only sees the known-device branch — ``register_known_device``
below.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.device import Device, DeviceStatus
from cms.models.device_profile import DeviceProfile
from cms.schemas.protocol import AuthAssignedMessage

logger = logging.getLogger("agora.cms.device_register")


# Mapping of device_type substrings to built-in profile names.
# Checked in order — more specific patterns first.
_DEVICE_TYPE_PROFILE_MAP = {
    "pi zero 2 w": "pi-zero-2w",
    "raspberry pi zero 2 w": "pi-zero-2w",
    "pi 5": "pi-5",
    "pi 4": "pi-4",
}


def hash_token(token: str) -> str:
    """SHA-256 hash of a token for DB storage."""
    return hashlib.sha256(token.encode()).hexdigest()


async def auto_assign_profile(device: Device, db: AsyncSession) -> None:
    """Auto-assign a device profile based on device_type if not already set."""
    if device.profile_id or not device.device_type:
        return

    dt_lower = device.device_type.lower()
    profile_name: str | None = None
    for pattern, name in _DEVICE_TYPE_PROFILE_MAP.items():
        if pattern in dt_lower:
            profile_name = name
            break

    if not profile_name:
        return

    result = await db.execute(
        select(DeviceProfile).where(DeviceProfile.name == profile_name)
    )
    profile = result.scalar_one_or_none()
    if profile:
        device.profile_id = profile.id
        await db.commit()
        logger.info("Auto-assigned profile '%s' to device %s", profile_name, device.id)


class RegisterResult:
    """Outcome of a known-device register.

    ``auth_assigned`` is a ready-to-send ``AuthAssignedMessage`` payload
    dict if the transport needs to push a new device_auth_token to the
    device; ``None`` otherwise.

    ``orphaned`` is True if the device failed auth — the transport
    should refuse the connection (direct-WS: close 4004; WPS: no-op
    because the Azure-side auth already succeeded, so the next message
    will just hit the same orphaned state).
    """

    __slots__ = ("auth_assigned", "orphaned")

    def __init__(
        self,
        *,
        auth_assigned: dict[str, Any] | None = None,
        orphaned: bool = False,
    ) -> None:
        self.auth_assigned = auth_assigned
        self.orphaned = orphaned


async def register_known_device(
    device: Device,
    raw: dict[str, Any],
    db: AsyncSession,
) -> RegisterResult:
    """Apply a ``register`` message from an already-provisioned device.

    Mirrors the ``else`` branch of the original register handler in
    ``cms/routers/ws.py``:

      * If the device already has an ``device_auth_token_hash`` set:
          - empty ``auth_token`` in the register payload → treat as a
            re-flashed device: reset to PENDING and assign a fresh
            auth token.
          - wrong ``auth_token`` → mark ORPHANED, return ``orphaned``.
          - correct token → accept; no new auth-assigned reply.
      * If the device has no stored token yet → mint one.

    Metadata (firmware/codecs/storage/last_seen/name) is refreshed
    from the register payload in all accepted cases.  Profile
    auto-assignment runs at the end.
    """
    auth_token = raw.get("auth_token", "")
    auth_assigned: dict[str, Any] | None = None

    if device.device_auth_token_hash:
        if not auth_token:
            # Empty token = device was re-flashed / factory reset.
            logger.info(
                "Device %s connected with empty token (likely re-flashed) "
                "— resetting to pending", device.id,
            )
            device.status = DeviceStatus.PENDING
            device.device_auth_token_hash = None
            device.last_seen = datetime.now(timezone.utc)
            await db.commit()

            new_token = secrets.token_urlsafe(32)
            device.device_auth_token_hash = hash_token(new_token)
            await db.commit()

            auth_assigned = AuthAssignedMessage(
                device_auth_token=new_token,
            ).model_dump(mode="json")
            logger.info("New auth token assigned to re-flashed device %s", device.id)
        elif hash_token(auth_token) != device.device_auth_token_hash:
            logger.warning("Device %s failed auth — marking as orphaned", device.id)
            device.status = DeviceStatus.ORPHANED
            device.last_seen = datetime.now(timezone.utc)
            await db.commit()
            return RegisterResult(orphaned=True)
    else:
        # Device row exists but no auth token yet — assign one.
        new_token = secrets.token_urlsafe(32)
        device.device_auth_token_hash = hash_token(new_token)
        await db.commit()
        auth_assigned = AuthAssignedMessage(
            device_auth_token=new_token,
        ).model_dump(mode="json")
        logger.info("Auth token assigned to existing device %s", device.id)

    # Refresh metadata from the register payload.
    device.firmware_version = raw.get("firmware_version", device.firmware_version)
    device.device_type = raw.get("device_type", device.device_type)
    reg_codecs = raw.get("supported_codecs")
    if reg_codecs is not None:
        device.supported_codecs = ",".join(reg_codecs)
    device.storage_capacity_mb = raw.get(
        "storage_capacity_mb", device.storage_capacity_mb,
    )
    device.storage_used_mb = raw.get("storage_used_mb", device.storage_used_mb)
    device.last_seen = datetime.now(timezone.utc)
    # Update name only if user explicitly set it via captive portal
    reg_name = raw.get("device_name", "")
    if reg_name and raw.get("device_name_custom", False):
        device.name = reg_name
    await db.commit()

    await auto_assign_profile(device, db)

    return RegisterResult(auth_assigned=auth_assigned)
