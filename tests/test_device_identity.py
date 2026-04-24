"""Unit tests for cms.services.device_identity.

Pure-function tests — no fixtures, no DB.  Exercises:

- Ed25519 sign/verify round trip (via the ``cryptography`` library's own
  signer on the device side).
- ECIES round trip (encrypt_for_device + decrypt with matching x25519).
- Fleet HMAC compute/verify + tamper detection.
- Timestamp skew helper.
- InMemoryNonceCache TTL + replay detection.
"""

from __future__ import annotations

import base64
import hashlib
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)
from cryptography.hazmat.primitives.asymmetric.x25519 import (
    X25519PrivateKey,
    X25519PublicKey,
)
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from cms.services.device_identity import (
    InMemoryNonceCache,
    _ECIES_HKDF_INFO,
    _ed25519_pub_to_x25519,
    compute_fleet_hmac,
    connect_token_canonical_bytes,
    encrypt_for_device,
    fleet_hmac_input,
    random_nonce,
    sha256_hex,
    timestamp_within_skew,
    verify_ed25519_signature,
    verify_fleet_hmac,
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _gen_ed25519():
    priv = Ed25519PrivateKey.generate()
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return priv, base64.b64encode(pub_bytes).decode("ascii")


def _decrypt_bootstrap(
    pubkey_b64: str, priv_ed: Ed25519PrivateKey, wire_b64: str,
) -> bytes:
    """Device-side ECIES decrypt, mirroring the firmware path."""
    wire = base64.b64decode(wire_b64)
    eph_pub_bytes, nonce, ct = wire[:32], wire[32:44], wire[44:]

    # Convert our ed25519 *private* key to x25519.  libsodium exposes
    # this as crypto_sign_ed25519_sk_to_curve25519 — we emulate by
    # hashing the seed and clamping per RFC 7748, which is exactly
    # what that function does internally.
    seed = priv_ed.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    h = hashlib.sha512(seed).digest()
    a = bytearray(h[:32])
    a[0] &= 248
    a[31] &= 127
    a[31] |= 64
    x_priv = X25519PrivateKey.from_private_bytes(bytes(a))

    eph_pub = X25519PublicKey.from_public_bytes(eph_pub_bytes)
    shared = x_priv.exchange(eph_pub)

    key_material = HKDF(
        algorithm=hashes.SHA256(),
        length=32 + 12,
        salt=None,
        info=_ECIES_HKDF_INFO,
    ).derive(shared)
    key, _expected_nonce = key_material[:32], key_material[32:]
    return AESGCM(key).decrypt(nonce, ct, associated_data=None)


# ---------------------------------------------------------------------
# Ed25519 signing
# ---------------------------------------------------------------------


class TestEd25519Signing:
    def test_round_trip_accepts_valid_signature(self):
        priv, pub_b64 = _gen_ed25519()
        msg = connect_token_canonical_bytes("dev-1", "2026-04-24T00:00:00Z", "abc")
        sig = priv.sign(msg)
        sig_b64 = base64.b64encode(sig).decode("ascii")

        assert verify_ed25519_signature(pub_b64, msg, sig_b64) is True

    def test_rejects_tampered_message(self):
        priv, pub_b64 = _gen_ed25519()
        msg = b"hello"
        sig_b64 = base64.b64encode(priv.sign(msg)).decode("ascii")

        assert verify_ed25519_signature(pub_b64, b"HELLO", sig_b64) is False

    def test_rejects_signature_from_wrong_key(self):
        priv_a, _ = _gen_ed25519()
        _, pub_b = _gen_ed25519()
        msg = b"hello"
        sig_b64 = base64.b64encode(priv_a.sign(msg)).decode("ascii")

        assert verify_ed25519_signature(pub_b, msg, sig_b64) is False

    @pytest.mark.parametrize(
        "bad_pub, bad_sig",
        [
            ("!!!not-b64!!!", "AAAA"),
            ("AAAA", "!!!not-b64!!!"),
            ("", ""),
            ("AAAA", "AAAA"),  # valid b64 but wrong length/material
        ],
    )
    def test_malformed_input_returns_false_not_raise(self, bad_pub, bad_sig):
        assert (
            verify_ed25519_signature(bad_pub, b"message", bad_sig) is False
        )


# ---------------------------------------------------------------------
# Canonical bytes
# ---------------------------------------------------------------------


def test_connect_token_canonical_is_pipe_delimited_utf8():
    out = connect_token_canonical_bytes("dev-1", "2026-04-24T00:00:00Z", "n1")
    assert out == b"dev-1|2026-04-24T00:00:00Z|n1"


def test_fleet_hmac_input_uses_fixed_schema():
    out = fleet_hmac_input(
        device_id="dev-1",
        pubkey="KEY",
        pairing_secret_hash="HASH",
        fleet_id="fleet-main",
        timestamp="1714000000",
        nonce="NONCE",
    )
    assert out == b"register|dev-1|KEY|HASH|fleet-main|1714000000|NONCE"


# ---------------------------------------------------------------------
# ECIES
# ---------------------------------------------------------------------


class TestECIES:
    def test_round_trip(self):
        priv, pub_b64 = _gen_ed25519()
        plaintext = b'{"wps_jwt":"abc","wps_url":"wss://x"}'

        wire = encrypt_for_device(pub_b64, plaintext)
        decrypted = _decrypt_bootstrap(pub_b64, priv, wire)

        assert decrypted == plaintext

    def test_different_keys_yield_different_ciphertext(self):
        _, pub_a = _gen_ed25519()
        _, pub_b = _gen_ed25519()
        pt = b"same plaintext"

        ct_a = encrypt_for_device(pub_a, pt)
        ct_b = encrypt_for_device(pub_b, pt)
        assert ct_a != ct_b

    def test_same_key_two_calls_use_different_ephemeral(self):
        """Ephemeral keypair must be fresh per call (IND-CPA)."""
        _, pub = _gen_ed25519()
        ct1 = encrypt_for_device(pub, b"x")
        ct2 = encrypt_for_device(pub, b"x")
        assert ct1 != ct2

    def test_wrong_private_key_cannot_decrypt(self):
        _priv_a, pub_a = _gen_ed25519()
        priv_b, _pub_b = _gen_ed25519()
        wire = encrypt_for_device(pub_a, b"secret")

        with pytest.raises(Exception):  # AESGCM tag verification fails
            _decrypt_bootstrap(pub_a, priv_b, wire)

    def test_rejects_malformed_base64_pubkey(self):
        with pytest.raises(ValueError):
            encrypt_for_device("not!valid!base64!@#$", b"x")

    def test_rejects_wrong_length_pubkey(self):
        short = base64.b64encode(b"\x00" * 16).decode("ascii")
        with pytest.raises(ValueError):
            encrypt_for_device(short, b"x")

    def test_rejects_non_canonical_y(self):
        # y = p (non-canonical encoding; field element not reduced mod p).
        p = (1 << 255) - 19
        bad = p.to_bytes(32, "little")
        bad_b64 = base64.b64encode(bad).decode("ascii")
        with pytest.raises(ValueError):
            encrypt_for_device(bad_b64, b"x")

    def test_rejects_y_equals_one(self):
        # y = 1 → denom (1-y) = 0; the Edwards identity point.
        one = (1).to_bytes(32, "little")
        b64 = base64.b64encode(one).decode("ascii")
        with pytest.raises(ValueError):
            encrypt_for_device(b64, b"x")

    def test_rejects_zero_pubkey(self):
        # y = 0 → u = (1+0)/(1-0) = 1, a low-order Montgomery point.
        zero = (0).to_bytes(32, "little")
        b64 = base64.b64encode(zero).decode("ascii")
        with pytest.raises(ValueError):
            encrypt_for_device(b64, b"x")

    def test_ed25519_to_x25519_matches_libsodium_reference(self):
        """Sanity check: converting an ed25519 pubkey to x25519 and doing
        a round-trip DH with the matching private key (via the same
        hashing trick firmware uses) yields a shared secret.
        """
        priv = Ed25519PrivateKey.generate()
        pub_bytes = priv.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        x_pub = _ed25519_pub_to_x25519(pub_bytes)

        # Build the matching x25519 private key from the ed25519 seed.
        seed = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        h = hashlib.sha512(seed).digest()
        a = bytearray(h[:32])
        a[0] &= 248
        a[31] &= 127
        a[31] |= 64
        x_priv = X25519PrivateKey.from_private_bytes(bytes(a))

        eph = X25519PrivateKey.generate()
        shared_sender = eph.exchange(x_pub)
        shared_recipient = x_priv.exchange(eph.public_key())
        assert shared_sender == shared_recipient


# ---------------------------------------------------------------------
# Fleet HMAC
# ---------------------------------------------------------------------


class TestFleetHmac:
    def test_compute_is_deterministic(self):
        assert compute_fleet_hmac(b"s", b"m") == compute_fleet_hmac(b"s", b"m")

    def test_verify_accepts_valid_mac(self):
        msg = fleet_hmac_input(
            device_id="d", pubkey="p", pairing_secret_hash="h",
            fleet_id="f", timestamp="1", nonce="n",
        )
        mac = compute_fleet_hmac(b"secret", msg)
        assert verify_fleet_hmac(b"secret", msg, mac) is True

    def test_verify_rejects_wrong_secret(self):
        msg = b"msg"
        mac = compute_fleet_hmac(b"right", msg)
        assert verify_fleet_hmac(b"wrong", msg, mac) is False

    def test_verify_rejects_tampered_message(self):
        mac = compute_fleet_hmac(b"s", b"msg")
        assert verify_fleet_hmac(b"s", b"MSG", mac) is False

    def test_verify_is_case_insensitive_on_hex(self):
        mac = compute_fleet_hmac(b"s", b"m")
        assert verify_fleet_hmac(b"s", b"m", mac.upper()) is True

    def test_verify_returns_false_on_non_string_mac(self):
        # Anonymous endpoint; must never 500 on bad input.
        mac = compute_fleet_hmac(b"s", b"m")
        assert verify_fleet_hmac(b"s", b"m", None) is False  # type: ignore[arg-type]
        assert verify_fleet_hmac(b"s", b"m", 12345) is False  # type: ignore[arg-type]
        assert verify_fleet_hmac(b"s", b"m", b"bytes") is False  # type: ignore[arg-type]
        # Sanity — still works for real mac.
        assert verify_fleet_hmac(b"s", b"m", mac) is True


# ---------------------------------------------------------------------
# Timestamp skew
# ---------------------------------------------------------------------


class TestTimestampSkew:
    def test_accepts_now(self):
        assert timestamp_within_skew(int(time.time()), 60) is True

    def test_rejects_far_past(self):
        assert timestamp_within_skew(int(time.time()) - 600, 60) is False

    def test_rejects_far_future(self):
        assert timestamp_within_skew(int(time.time()) + 600, 60) is False

    def test_accepts_edge_of_window(self):
        assert timestamp_within_skew(int(time.time()) - 60, 60) is True


# ---------------------------------------------------------------------
# InMemoryNonceCache
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestInMemoryNonceCache:
    async def test_first_use_accepted(self):
        c = InMemoryNonceCache(ttl_seconds=60)
        assert await c.check_and_record("connect-token", "n1") is True

    async def test_replay_rejected(self):
        c = InMemoryNonceCache(ttl_seconds=60)
        await c.check_and_record("connect-token", "n1")
        assert await c.check_and_record("connect-token", "n1") is False

    async def test_different_scope_does_not_collide(self):
        c = InMemoryNonceCache(ttl_seconds=60)
        await c.check_and_record("fleet", "n1")
        assert await c.check_and_record("connect-token", "n1") is True

    async def test_expired_entry_replayable(self):
        c = InMemoryNonceCache(ttl_seconds=0)
        await c.check_and_record("s", "n1")
        # TTL is 0 → expired on next gc sweep
        await _sleep_ms(20)
        assert await c.check_and_record("s", "n1") is True

    async def test_seen_does_not_record(self):
        c = InMemoryNonceCache(ttl_seconds=60)
        assert await c.seen("s", "n1") is False
        assert await c.seen("s", "n1") is False

    async def test_record_then_seen(self):
        c = InMemoryNonceCache(ttl_seconds=60)
        await c.record("s", "n1")
        assert await c.seen("s", "n1") is True


async def _sleep_ms(ms: int):
    import asyncio as _asyncio
    await _asyncio.sleep(ms / 1000.0)


# ---------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------


def test_sha256_hex_matches_hashlib():
    assert sha256_hex(b"abc") == hashlib.sha256(b"abc").hexdigest()


def test_random_nonce_length_and_hex():
    n = random_nonce(16)
    assert len(n) == 32
    int(n, 16)  # valid hex


def test_random_nonce_is_actually_random():
    assert random_nonce() != random_nonce()
