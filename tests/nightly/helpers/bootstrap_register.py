"""Helper for driving the bootstrap ``/register`` HMAC flow from nightly tests.

The nightly stack provisions a single fleet (id=``nightly-fleet``) with a
fixed base64 secret in ``docker-compose.nightly.yml``.  This module
re-implements the device-side registration just enough to land a row in
the ``pending_registrations`` table from a Playwright test.

Pairs with ``cms.services.device_identity.fleet_hmac_input`` — the
canonical MAC string MUST stay byte-for-byte identical or /register
returns 401.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from playwright.sync_api import APIRequestContext


# The nightly conftest seeds this fleet row in the ``fleets`` table
# during stack startup (see ``_seed_nightly_fleet``).  The constants
# below are the source of truth used both for that insert and the
# /register HMAC computation.
NIGHTLY_FLEET_ID = "nightly-fleet"
_NIGHTLY_FLEET_SECRET_RAW = b"nightly-fleet-secret-32bytes!!!!"
NIGHTLY_FLEET_SECRET_B64 = base64.b64encode(_NIGHTLY_FLEET_SECRET_RAW).decode("ascii")


def _fleet_hmac_input(
    *,
    device_id: str,
    pubkey: str,
    pairing_secret_hash: str,
    fleet_id: str,
    timestamp: str,
    nonce: str,
) -> bytes:
    return "|".join(
        ["register", device_id, pubkey, pairing_secret_hash, fleet_id, timestamp, nonce]
    ).encode("utf-8")


@dataclass
class RegisteredDevice:
    """The bits a test needs after a successful /register call."""

    device_id: str
    pubkey_b64: str
    pairing_secret: str  # plaintext (admin would scan from QR)
    pairing_secret_hash: str  # hex sha256 of pairing_secret


def register_pending_device(
    request: APIRequestContext,
    *,
    device_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    fleet_id: str = NIGHTLY_FLEET_ID,
    fleet_secret_b64: str = NIGHTLY_FLEET_SECRET_B64,
) -> RegisteredDevice:
    """Issue an HMAC-authed POST /api/devices/register against the live CMS.

    Returns the keypair + pairing material so the caller can also exercise
    the adopt flow if desired.  Raises ``AssertionError`` with the response
    body on any non-2xx, so test failures point at the actual server error.
    """
    if device_id is None:
        device_id = f"nightly-pending-{uuid.uuid4().hex[:12]}"

    priv = Ed25519PrivateKey.generate()
    pub_raw = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pubkey_b64 = base64.b64encode(pub_raw).decode("ascii")

    pairing_secret = uuid.uuid4().hex
    pairing_hash = hashlib.sha256(pairing_secret.encode("utf-8")).hexdigest()

    timestamp = str(int(time.time()))
    nonce = uuid.uuid4().hex
    secret_bytes = base64.b64decode(fleet_secret_b64)
    canonical = _fleet_hmac_input(
        device_id=device_id,
        pubkey=pubkey_b64,
        pairing_secret_hash=pairing_hash,
        fleet_id=fleet_id,
        timestamp=timestamp,
        nonce=nonce,
    )
    mac = hmac.new(secret_bytes, canonical, hashlib.sha256).hexdigest()

    body: dict[str, Any] = {
        "device_id": device_id,
        "pubkey": pubkey_b64,
        "pairing_secret_hash": pairing_hash,
        "metadata": metadata or {"board": "pi5", "source": "nightly-pending-test"},
    }
    headers = {
        "X-Fleet-Id": fleet_id,
        "X-Fleet-Timestamp": timestamp,
        "X-Fleet-Nonce": nonce,
        "X-Fleet-Mac": mac,
        "Content-Type": "application/json",
    }
    resp = request.post("/api/devices/register", data=body, headers=headers)
    # Router returns 202 Accepted -- the registration is now pending operator review.
    assert resp.status == 202, (
        f"POST /api/devices/register -> {resp.status}: {resp.text()[:500]}"
    )
    return RegisteredDevice(
        device_id=device_id,
        pubkey_b64=pubkey_b64,
        pairing_secret=pairing_secret,
        pairing_secret_hash=pairing_hash,
    )
