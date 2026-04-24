"""Cryptographic primitives for the HTTPS device-bootstrap flow.

Pure helpers for the three signature/encryption surfaces introduced by
the bootstrap redesign (umbrella issue #420):

1. **Ed25519 request signing** — devices sign canonicalised bytes when
   hitting ``POST /api/devices/connect-token`` (no transport-level
   authentication; the signature is the auth).
2. **ECIES encryption to a device's identity key** — CMS encrypts the
   bootstrap outbox payload (WPS URL + JWT) with the device's public key
   so the reply to the anonymous ``GET /api/devices/bootstrap-status``
   endpoint is useless to anyone but the device.
3. **Fleet HMAC verification** — ``/api/devices/register`` is anonymous
   but gated by a shared per-fleet secret so random internet scanners
   can't create junk pending_registrations rows.

Plus an in-memory TTL nonce cache used to block replay of both signed
``/connect-token`` requests and fleet-HMAC ``/register`` requests.

This module is intentionally self-contained and has **no FastAPI /
SQLAlchemy / asyncio dependencies beyond an asyncio.Lock for the nonce
cache** so it can be unit-tested in isolation.

Nonce cache scope note
----------------------
The default :class:`InMemoryNonceCache` is a **per-process** cache.  It
is only safe when the CMS runs as a single process with a single asyncio
event loop.  In particular it is **not** safe under:

- ``N > 1`` replicas (each replica has its own cache)
- multiple uvicorn workers in one container
- overlapping rolling deploys (two revisions active simultaneously)
- blue/green deploys

Stage 4 of the multi-replica rollout (tracked in ``#344``) will replace
this with a DB-backed cache via the :class:`NonceCache` protocol before
``minReplicas`` is bumped past 1.  Until then the CMS is pinned to
``minReplicas=maxReplicas=1`` and this cache is the authoritative
replay-protection primitive.

Under ``N > 1`` without a shared cache, an attacker who captured a valid
signed request in flight would get an ``N`` replay window (one replay
per replica).  Damage is bounded by:

- the 60s timestamp skew on ``/connect-token`` (replay must land in
  one minute),
- the 300s skew on fleet HMAC (five minutes),
- the rate limits on both endpoints,
- and, for ``/connect-token``, the fact that a replay only re-mints a
  WPS JWT for the device that already owns the signature — no
  privilege escalation.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import hmac
import os
import time
from dataclasses import dataclass
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


# ---------------------------------------------------------------------
# Ed25519 request signing
# ---------------------------------------------------------------------


def connect_token_canonical_bytes(
    device_id: str, timestamp: str, nonce: str,
) -> bytes:
    """Canonical byte representation of a ``/connect-token`` request.

    Must be bit-identical between firmware signer and CMS verifier.
    Fixed schema, pipe-delimited — no JSON canonicalisation needed.
    """
    return f"{device_id}|{timestamp}|{nonce}".encode("utf-8")


def verify_ed25519_signature(
    pubkey_b64: str, message: bytes, signature_b64: str,
) -> bool:
    """Return ``True`` iff ``signature_b64`` is a valid ed25519 signature
    over ``message`` under ``pubkey_b64``.

    Never raises on malformed input — returns ``False`` instead so the
    caller can emit a uniform ``401`` without leaking which specific
    field was bad.
    """
    try:
        pub = Ed25519PublicKey.from_public_bytes(base64.b64decode(pubkey_b64))
        sig = base64.b64decode(signature_b64)
        pub.verify(sig, message)
    except (ValueError, InvalidSignature, TypeError):
        return False
    return True


# ---------------------------------------------------------------------
# Ed25519 → X25519 for ECIES
# ---------------------------------------------------------------------


def _ed25519_pub_to_x25519(pub_bytes: bytes) -> X25519PublicKey:
    """Convert an ed25519 public key to the matching X25519 public key.

    Standard curve isomorphism: ``cryptography`` doesn't expose a direct
    converter, but we can round-trip via the raw Edwards point.  PyNaCl
    has a one-liner (``crypto_sign_ed25519_pk_to_curve25519``); we
    replicate it with the standard library.
    """
    # Reference: RFC 7748 / libsodium crypto_sign_ed25519_pk_to_curve25519.
    # The Montgomery u-coordinate is (1 + y) / (1 - y) mod p with y
    # recovered from the encoded point.  We defer to the standard
    # implementation in ``cryptography``'s internals... but since that
    # isn't public API, use the math directly.
    if len(pub_bytes) != 32:
        raise ValueError("ed25519 public key must be 32 bytes")
    # Decode little-endian y with the sign bit masked off.
    y = int.from_bytes(pub_bytes, "little") & ((1 << 255) - 1)
    p = (1 << 255) - 19
    # Reject non-canonical encodings (y must be reduced mod p).
    if y >= p:
        raise ValueError("ed25519 public key y-coordinate is not canonical")
    # u = (1 + y) / (1 - y) mod p
    denom = (1 - y) % p
    if denom == 0:
        # y == 1 → point at infinity on the Edwards curve.
        raise ValueError("ed25519 public key maps to point at infinity")
    inv = pow(denom, p - 2, p)
    u = ((1 + y) * inv) % p
    # Reject degenerate/low-order u values (u == 0 is the identity on the
    # Montgomery curve; u == 1 is also a known low-order point).
    if u in (0, 1):
        raise ValueError("ed25519 public key maps to a low-order x25519 point")
    u_bytes = u.to_bytes(32, "little")
    return X25519PublicKey.from_public_bytes(u_bytes)


# ---------------------------------------------------------------------
# ECIES to an ed25519 recipient
# ---------------------------------------------------------------------


_ECIES_HKDF_INFO = b"agora-bootstrap-ecies-v1"


def encrypt_for_device(pubkey_b64: str, plaintext: bytes) -> str:
    """ECIES-encrypt ``plaintext`` for the holder of ``pubkey_b64``.

    Wire format (base64 of the concatenation):

        ``ephemeral_x25519_pub (32B) || nonce (12B) || ciphertext || tag (16B)``

    The AES-GCM tag is appended to the ciphertext by ``AESGCM.encrypt``,
    so the layout is effectively ``[32 || 12 || ciphertext_with_tag]``.
    """
    try:
        recip_ed = base64.b64decode(pubkey_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("invalid base64 recipient public key") from e
    recip_x = _ed25519_pub_to_x25519(recip_ed)

    eph_priv = X25519PrivateKey.generate()
    eph_pub_bytes = eph_priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    shared = eph_priv.exchange(recip_x)
    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=32 + 12,
        salt=None,
        info=_ECIES_HKDF_INFO,
    ).derive(shared)
    key, nonce = key_material[:32], key_material[32:]

    ct = AESGCM(key).encrypt(nonce, plaintext, associated_data=None)
    return base64.b64encode(eph_pub_bytes + nonce + ct).decode("ascii")


def _ed25519_priv_to_x25519(priv_bytes: bytes) -> X25519PrivateKey:
    """Convert a 32-byte raw ed25519 private key to the X25519 scalar.

    Counterpart to :func:`_ed25519_pub_to_x25519` — applies the same
    RFC8032 derivation the firmware uses.
    """
    if len(priv_bytes) != 32:
        raise ValueError("ed25519 private key must be 32 bytes")
    h = hashlib.sha512(priv_bytes).digest()[:32]
    # Clamp per RFC 7748 §5.
    scalar = bytearray(h)
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    return X25519PrivateKey.from_private_bytes(bytes(scalar))


def decrypt_with_device_key(priv_bytes: bytes, ciphertext_b64: str) -> bytes:
    """Inverse of :func:`encrypt_for_device`.

    Takes the 32-byte raw ed25519 private key held by the device and
    returns the plaintext payload.  Used by the device-side client
    and by tests.
    """
    try:
        blob = base64.b64decode(ciphertext_b64, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("invalid base64 ciphertext") from e
    if len(blob) < 32 + 12 + 16:
        raise ValueError("ciphertext too short")
    eph_pub, nonce, ct = blob[:32], blob[32:44], blob[44:]
    x_priv = _ed25519_priv_to_x25519(priv_bytes)
    shared = x_priv.exchange(X25519PublicKey.from_public_bytes(eph_pub))
    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=32 + 12,
        salt=None,
        info=_ECIES_HKDF_INFO,
    ).derive(shared)
    key, derived_nonce = key_material[:32], key_material[32:]
    if derived_nonce != nonce:
        raise ValueError("nonce mismatch")
    return AESGCM(key).decrypt(nonce, ct, associated_data=None)


# ---------------------------------------------------------------------
# Fleet HMAC
# ---------------------------------------------------------------------


def fleet_hmac_input(
    *,
    device_id: str,
    pubkey: str,
    pairing_secret_hash: str,
    fleet_id: str,
    timestamp: str,
    nonce: str,
) -> bytes:
    """Canonical MAC input for ``POST /api/devices/register``.

    Must match the device-side constructor byte-for-byte.  Schema is
    frozen as of issue #420; any future change becomes a breaking
    firmware bump.
    """
    parts = [
        "register",
        device_id,
        pubkey,
        pairing_secret_hash,
        fleet_id,
        timestamp,
        nonce,
    ]
    return "|".join(parts).encode("utf-8")


def compute_fleet_hmac(secret: bytes, message: bytes) -> str:
    """HMAC-SHA256 of ``message`` under ``secret``, hex-encoded."""
    return hmac.new(secret, message, hashlib.sha256).hexdigest()


def verify_fleet_hmac(
    secret: bytes, message: bytes, mac_hex: str,
) -> bool:
    """Constant-time verification of a fleet HMAC.  Never raises."""
    if not isinstance(mac_hex, str):
        return False
    try:
        expected = compute_fleet_hmac(secret, message)
        return hmac.compare_digest(expected, mac_hex.lower())
    except (TypeError, ValueError, AttributeError):
        return False


# ---------------------------------------------------------------------
# Timestamp skew helpers
# ---------------------------------------------------------------------


def timestamp_within_skew(ts_unix: int, skew_seconds: int) -> bool:
    """Return True if ``ts_unix`` is within ``±skew_seconds`` of now."""
    return abs(int(time.time()) - int(ts_unix)) <= int(skew_seconds)


# ---------------------------------------------------------------------
# Nonce cache (replay protection)
# ---------------------------------------------------------------------


class NonceCache(Protocol):
    """Protocol for replay-protection caches.

    Implementations must be safe to call from async FastAPI handlers.
    A cache entry is keyed by ``scope`` (e.g. ``"fleet"``,
    ``"connect-token"``) to prevent cross-endpoint collisions.

    Callers should use :meth:`check_and_record` — a single atomic call
    that both checks for replay and records the nonce.  The separate
    ``seen`` / ``record`` methods exist for diagnostics / tests only;
    using them in a hot path is a TOCTOU bug waiting to happen.
    """

    async def check_and_record(self, scope: str, nonce: str) -> bool:
        """Atomic seen+record.  Returns True if fresh (accept),
        False if replay (reject).
        """
        ...

    async def seen(self, scope: str, nonce: str) -> bool: ...
    async def record(self, scope: str, nonce: str) -> None: ...


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float


class InMemoryNonceCache:
    """Simple per-process TTL nonce cache.

    Safe under asyncio concurrency; not shared across replicas.  See the
    module docstring for replay-window implications.
    """

    def __init__(self, ttl_seconds: int = 600) -> None:
        self._ttl = ttl_seconds
        self._entries: dict[tuple[str, str], _CacheEntry] = {}
        self._lock = asyncio.Lock()

    async def seen(self, scope: str, nonce: str) -> bool:
        async with self._lock:
            self._gc_locked()
            return (scope, nonce) in self._entries

    async def record(self, scope: str, nonce: str) -> None:
        async with self._lock:
            self._entries[(scope, nonce)] = _CacheEntry(
                expires_at=time.time() + self._ttl,
            )

    async def check_and_record(self, scope: str, nonce: str) -> bool:
        """Atomic ``seen`` + ``record``.  Returns True if accepted
        (nonce was fresh), False if it was a replay.
        """
        async with self._lock:
            self._gc_locked()
            key = (scope, nonce)
            if key in self._entries:
                return False
            self._entries[key] = _CacheEntry(
                expires_at=time.time() + self._ttl,
            )
            return True

    def _gc_locked(self) -> None:
        now = time.time()
        expired = [k for k, v in self._entries.items() if v.expires_at <= now]
        for k in expired:
            self._entries.pop(k, None)


# ---------------------------------------------------------------------
# Misc helpers
# ---------------------------------------------------------------------


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def random_nonce(nbytes: int = 16) -> str:
    """Hex-encoded cryptographically random nonce."""
    return os.urandom(nbytes).hex()


def canonicalize_pubkey_b64(s: str) -> str:
    """Normalize a user-provided ed25519 pubkey encoding.

    Accepts either standard base64 (``+/``) or URL-safe base64 (``-_``),
    with or without padding.  Decodes and re-encodes using the standard
    base64 alphabet with ``=`` padding so the resulting string is
    byte-for-byte identical regardless of which form the caller used.

    This lets ``POST /register`` (JSON body) and ``GET /bootstrap-status?pubkey=…``
    (URL query where ``+`` is painful) agree on the same lookup key.

    Raises ``ValueError`` if the input isn't valid base64 of a 32-byte
    ed25519 public key.
    """
    if not isinstance(s, str):
        raise ValueError("pubkey must be a string")
    # Map urlsafe alphabet to standard, then add missing padding.
    t = s.replace("-", "+").replace("_", "/")
    # Base64 requires length to be a multiple of 4.
    pad = (-len(t)) % 4
    t = t + ("=" * pad)
    try:
        raw = base64.b64decode(t, validate=True)
    except (binascii.Error, ValueError) as e:
        raise ValueError("pubkey is not valid base64") from e
    if len(raw) != 32:
        raise ValueError("ed25519 public key must decode to 32 bytes")
    return base64.b64encode(raw).decode("ascii")
