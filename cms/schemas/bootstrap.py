"""Pydantic request/response models for the HTTPS bootstrap endpoints.

See ``cms.routers.bootstrap`` and umbrella issue #420.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


# ---------------------------------------------------------------------
# POST /api/devices/register
# ---------------------------------------------------------------------


class RegisterRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    # Standard base64 encoding of the 32-byte ed25519 public key.
    pubkey: str = Field(min_length=1, max_length=128)
    # Hex SHA-256 digest of the raw pairing secret the device shows in
    # its QR code.  64 hex characters.
    pairing_secret_hash: str = Field(pattern=r"^[0-9a-fA-F]{64}$")
    metadata: dict[str, Any] = Field(default_factory=dict)


class RegisterResponse(BaseModel):
    status: str = "pending"


# ---------------------------------------------------------------------
# GET /api/devices/bootstrap-status
# ---------------------------------------------------------------------


class BootstrapStatusResponse(BaseModel):
    status: str  # "pending" | "adopted"
    payload: str | None = None  # base64 ECIES ciphertext when adopted


# ---------------------------------------------------------------------
# POST /api/devices/adopt (new bootstrap-QR flow)
# ---------------------------------------------------------------------


class BootstrapAdoptRequest(BaseModel):
    pairing_secret: str = Field(min_length=8, max_length=128)
    name: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=255)
    group_id: str | None = None  # uuid
    profile_id: str = Field(min_length=1)  # uuid; required


class AdoptPendingRequest(BaseModel):
    """Request body for ``POST /api/devices/adopt-pending``.

    Identifies the pending row by its primary key rather than the
    pairing secret, so the admin never has to see or type the secret.
    """

    pending_id: str = Field(min_length=1)  # uuid
    name: str | None = Field(default=None, max_length=100)
    location: str | None = Field(default=None, max_length=255)
    group_id: str | None = None  # uuid
    profile_id: str = Field(min_length=1)  # uuid; required


class BootstrapAdoptResponse(BaseModel):
    device_id: str
    status: str = "adopted"


class PendingDeviceSummary(BaseModel):
    """One row of the pending-devices list surfaced to admins.

    Excludes ``pairing_secret_hash`` and ``outbox_ciphertext`` — those
    are on-wire secrets the admin has no reason to see.  ``pubkey`` is
    returned in full-string form but the UI only renders a short
    fingerprint; full value is handy for support/debugging.
    """

    id: str  # uuid primary key, used to adopt via /adopt-pending
    device_id: str  # device-chosen (Pi serial/hostname), advisory
    pubkey: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    ip_address: str | None = None
    created_at: str  # ISO 8601
    polled_at: str | None = None  # ISO 8601
    has_polled: bool = False


class PendingDevicesResponse(BaseModel):
    items: list[PendingDeviceSummary]


# ---------------------------------------------------------------------
# POST /api/devices/connect-token
# ---------------------------------------------------------------------


class ConnectTokenRequest(BaseModel):
    device_id: str = Field(min_length=1, max_length=64)
    # Unix seconds as an integer.  A raw int avoids the ISO-8601 parse
    # ambiguity around timezones / precision / canonicalization — the
    # canonical bytes that get signed are ``f"{device_id}|{timestamp}|{nonce}"``
    # and both sides can trivially agree on an integer decimal string.
    timestamp: int
    # Random hex nonce.  16 bytes = 32 hex chars is the minimum we accept.
    nonce: str = Field(min_length=32, max_length=128, pattern=r"^[0-9a-fA-F]+$")
    # Base64 ed25519 signature over the canonical bytes.
    signature: str = Field(min_length=1, max_length=128)


class ConnectTokenResponse(BaseModel):
    wps_jwt: str
    wps_url: str
    expires_at: str  # RFC3339 UTC
