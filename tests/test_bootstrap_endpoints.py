"""Integration tests for the HTTPS bootstrap + connect-token endpoints.

Covers the behaviors spelled out in the umbrella issue #420 Stage A.3
plan, plus the additional edge cases surfaced during rubber-duck review:

- /register: fleet-HMAC happy path, missing/bad HMAC, stale timestamp,
  replayed nonce, re-registration upsert, cap enforcement, pubkey
  canonicalisation on write, duplicate-pubkey partial-unique-index.
- /bootstrap-status: pending vs adopted, unknown pubkey, polled_at
  latching, pubkey encoding tolerance.
- /adopt: admin happy path, bad secret, already adopted,
  unknown group/profile, audit log row.
- /connect-token: valid signature, unknown device, revoked pubkey,
  tampered signature, stale timestamp, replay.

Slowapi / in-memory rate-limit buckets are process-global; several
tests explicitly reset them to avoid leakage between cases.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
import uuid

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
)

pytestmark = pytest.mark.asyncio

from cms.routers import bootstrap as bootstrap_router_mod
from cms.services import device_identity


FLEET_ID = "test-fleet"
FLEET_SECRET_BYTES = b"\x11" * 32
FLEET_SECRET_B64 = base64.b64encode(FLEET_SECRET_BYTES).decode("ascii")


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------


def _reset_rate_limit_buckets() -> None:
    bootstrap_router_mod._buckets.clear()


def _gen_keypair():
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    pub_raw = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    pub_b64 = base64.b64encode(pub_raw).decode("ascii")
    return priv, pub_b64


def _fleet_headers(
    *,
    device_id: str,
    pubkey_b64: str,
    pairing_secret_hash_hex: str,
    fleet_id: str = FLEET_ID,
    secret: bytes = FLEET_SECRET_BYTES,
    timestamp: int | None = None,
    nonce: str | None = None,
) -> dict[str, str]:
    if timestamp is None:
        timestamp = int(time.time())
    if nonce is None:
        nonce = uuid.uuid4().hex
    canonical = device_identity.fleet_hmac_input(
        device_id=device_id,
        pubkey=pubkey_b64,
        pairing_secret_hash=pairing_secret_hash_hex,
        fleet_id=fleet_id,
        timestamp=str(timestamp),
        nonce=nonce,
    )
    mac = device_identity.compute_fleet_hmac(secret, canonical)
    return {
        "X-Fleet-Id": fleet_id,
        "X-Fleet-Timestamp": str(timestamp),
        "X-Fleet-Nonce": nonce,
        "X-Fleet-Mac": mac,
    }


def _pairing_pair() -> tuple[str, str]:
    secret = uuid.uuid4().hex
    return secret, hashlib.sha256(secret.encode("utf-8")).hexdigest()


@pytest.fixture
def fleet_secret_enabled(monkeypatch, app):
    """Inject ``FLEET_REGISTER_SECRETS`` into the app's overridden settings."""
    from cms.auth import get_settings

    override = app.dependency_overrides[get_settings]
    real_settings = override()
    real_settings.fleet_register_secrets = {FLEET_ID: FLEET_SECRET_B64}
    real_settings.pending_registrations_max = 10_000
    yield real_settings
    real_settings.fleet_register_secrets = {}


@pytest.fixture(autouse=True)
def _reset_buckets_between_tests():
    _reset_rate_limit_buckets()
    yield
    _reset_rate_limit_buckets()


# ---------------------------------------------------------------------
# /register
# ---------------------------------------------------------------------


class TestRegister:
    async def test_happy_path_creates_pending(
        self, unauthed_client, fleet_secret_enabled, db_session,
    ):
        _, pub_b64 = _gen_keypair()
        pairing_secret, pairing_hash = _pairing_pair()

        body = {
            "device_id": "raspberrypi-001",
            "pubkey": pub_b64,
            "pairing_secret_hash": pairing_hash,
            "metadata": {"board": "pi5", "mac": "aa:bb:cc:dd:ee:ff"},
        }
        headers = _fleet_headers(
            device_id=body["device_id"],
            pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
        )
        resp = await unauthed_client.post(
            "/api/devices/register", json=body, headers=headers,
        )
        assert resp.status_code == 202, resp.text
        assert resp.json() == {"status": "pending"}

        from cms.models.pending_registration import PendingRegistration
        from sqlalchemy import select
        row = (
            await db_session.execute(
                select(PendingRegistration).where(
                    PendingRegistration.pairing_secret_hash == pairing_hash,
                )
            )
        ).scalar_one()
        assert row.pubkey == pub_b64
        assert row.device_id == "raspberrypi-001"
        assert row.adopted_at is None

    async def test_client_ip_uses_x_forwarded_for(
        self, unauthed_client, fleet_secret_enabled, db_session,
    ):
        """The first entry of X-Forwarded-For must be stored as the device's
        client IP, not the Container Apps ingress hop in request.client.host."""
        _, pub_b64 = _gen_keypair()
        _, pairing_hash = _pairing_pair()
        body = {
            "device_id": "xff-test-pi",
            "pubkey": pub_b64,
            "pairing_secret_hash": pairing_hash,
        }
        headers = _fleet_headers(
            device_id=body["device_id"],
            pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
        )
        headers["X-Forwarded-For"] = "203.0.113.42, 10.0.0.5"
        resp = await unauthed_client.post(
            "/api/devices/register", json=body, headers=headers,
        )
        assert resp.status_code == 202, resp.text

        from cms.models.pending_registration import PendingRegistration
        from sqlalchemy import select
        row = (
            await db_session.execute(
                select(PendingRegistration).where(
                    PendingRegistration.device_id == "xff-test-pi",
                )
            )
        ).scalar_one()
        assert row.ip_address == "203.0.113.42"

    async def test_missing_fleet_headers_rejected(
        self, unauthed_client, fleet_secret_enabled,
    ):
        _, pub_b64 = _gen_keypair()
        _, pairing_hash = _pairing_pair()
        resp = await unauthed_client.post(
            "/api/devices/register",
            json={
                "device_id": "pi-x", "pubkey": pub_b64,
                "pairing_secret_hash": pairing_hash,
            },
        )
        assert resp.status_code == 401

    async def test_bad_hmac_rejected(
        self, unauthed_client, fleet_secret_enabled,
    ):
        _, pub_b64 = _gen_keypair()
        _, pairing_hash = _pairing_pair()
        headers = _fleet_headers(
            device_id="pi-x", pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
            secret=b"\x00" * 32,  # wrong secret
        )
        resp = await unauthed_client.post(
            "/api/devices/register",
            json={
                "device_id": "pi-x", "pubkey": pub_b64,
                "pairing_secret_hash": pairing_hash,
            },
            headers=headers,
        )
        assert resp.status_code == 401

    async def test_stale_timestamp_rejected(
        self, unauthed_client, fleet_secret_enabled,
    ):
        _, pub_b64 = _gen_keypair()
        _, pairing_hash = _pairing_pair()
        headers = _fleet_headers(
            device_id="pi-x", pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
            timestamp=int(time.time()) - 3600,  # way outside ±300s
        )
        resp = await unauthed_client.post(
            "/api/devices/register",
            json={
                "device_id": "pi-x", "pubkey": pub_b64,
                "pairing_secret_hash": pairing_hash,
            },
            headers=headers,
        )
        assert resp.status_code == 401

    async def test_replayed_nonce_rejected(
        self, unauthed_client, fleet_secret_enabled,
    ):
        _, pub_b64 = _gen_keypair()
        _, pairing_hash = _pairing_pair()
        headers = _fleet_headers(
            device_id="pi-x", pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
        )
        body = {
            "device_id": "pi-x", "pubkey": pub_b64,
            "pairing_secret_hash": pairing_hash,
        }
        r1 = await unauthed_client.post(
            "/api/devices/register", json=body, headers=headers,
        )
        assert r1.status_code == 202
        r2 = await unauthed_client.post(
            "/api/devices/register", json=body, headers=headers,
        )
        assert r2.status_code == 401

    async def test_re_registration_same_hash_is_upsert(
        self, unauthed_client, fleet_secret_enabled, db_session,
    ):
        """Device that replays /register after a restart keeps the same
        ``pending_registrations`` row (matched by pairing_secret_hash).
        """
        _, pub_b64 = _gen_keypair()
        pairing_secret, pairing_hash = _pairing_pair()

        body = {
            "device_id": "pi-x", "pubkey": pub_b64,
            "pairing_secret_hash": pairing_hash,
            "metadata": {"v": 1},
        }
        h1 = _fleet_headers(
            device_id=body["device_id"], pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
        )
        r1 = await unauthed_client.post(
            "/api/devices/register", json=body, headers=h1,
        )
        assert r1.status_code == 202

        body["metadata"] = {"v": 2}
        h2 = _fleet_headers(
            device_id=body["device_id"], pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
        )
        r2 = await unauthed_client.post(
            "/api/devices/register", json=body, headers=h2,
        )
        assert r2.status_code == 202

        from cms.models.pending_registration import PendingRegistration
        from sqlalchemy import select, func
        count = (
            await db_session.execute(
                select(func.count()).select_from(PendingRegistration).where(
                    PendingRegistration.pairing_secret_hash == pairing_hash,
                )
            )
        ).scalar_one()
        assert count == 1
        row = (
            await db_session.execute(
                select(PendingRegistration).where(
                    PendingRegistration.pairing_secret_hash == pairing_hash,
                )
            )
        ).scalar_one()
        assert row.connection_metadata == {"v": 2}

    async def test_cap_returns_503(
        self, unauthed_client, fleet_secret_enabled,
    ):
        fleet_secret_enabled.pending_registrations_max = 0  # 0 means cap=0 → reject all

        _, pub_b64 = _gen_keypair()
        _, pairing_hash = _pairing_pair()
        # The cap=0 config means 0 > 0 is false — safely override to 1 w/ one already present.
        # Easier: set cap to 1 and force two different pairs.
        fleet_secret_enabled.pending_registrations_max = 1

        headers = _fleet_headers(
            device_id="pi-a", pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
        )
        r1 = await unauthed_client.post(
            "/api/devices/register",
            json={"device_id": "pi-a", "pubkey": pub_b64,
                  "pairing_secret_hash": pairing_hash},
            headers=headers,
        )
        assert r1.status_code == 202

        _, pub_b64_b = _gen_keypair()
        _, hash_b = _pairing_pair()
        headers_b = _fleet_headers(
            device_id="pi-b", pubkey_b64=pub_b64_b,
            pairing_secret_hash_hex=hash_b,
        )
        r2 = await unauthed_client.post(
            "/api/devices/register",
            json={"device_id": "pi-b", "pubkey": pub_b64_b,
                  "pairing_secret_hash": hash_b},
            headers=headers_b,
        )
        assert r2.status_code == 503


# ---------------------------------------------------------------------
# /bootstrap-status
# ---------------------------------------------------------------------


class TestBootstrapStatus:
    async def test_unknown_pubkey_returns_404(self, unauthed_client):
        _, pub_b64 = _gen_keypair()
        resp = await unauthed_client.get(
            "/api/devices/bootstrap-status", params={"pubkey": pub_b64},
        )
        assert resp.status_code == 404

    async def test_pending_returns_pending_and_latches_polled_at(
        self, unauthed_client, fleet_secret_enabled, db_session,
    ):
        _, pub_b64 = _gen_keypair()
        _, pairing_hash = _pairing_pair()
        headers = _fleet_headers(
            device_id="pi-x", pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
        )
        await unauthed_client.post(
            "/api/devices/register",
            json={"device_id": "pi-x", "pubkey": pub_b64,
                  "pairing_secret_hash": pairing_hash},
            headers=headers,
        )

        r1 = await unauthed_client.get(
            "/api/devices/bootstrap-status", params={"pubkey": pub_b64},
        )
        assert r1.status_code == 200
        assert r1.json() == {"status": "pending", "payload": None}

        from cms.models.pending_registration import PendingRegistration
        from sqlalchemy import select
        row = (
            await db_session.execute(
                select(PendingRegistration).where(
                    PendingRegistration.pubkey == pub_b64,
                )
            )
        ).scalar_one()
        first_poll = row.polled_at
        assert first_poll is not None

        r2 = await unauthed_client.get(
            "/api/devices/bootstrap-status", params={"pubkey": pub_b64},
        )
        assert r2.status_code == 200
        await db_session.refresh(row)
        # polled_at should not have been bumped by a subsequent poll.
        assert row.polled_at == first_poll

    async def test_urlsafe_pubkey_normalised_on_lookup(
        self, unauthed_client, fleet_secret_enabled,
    ):
        _, pub_b64 = _gen_keypair()
        _, pairing_hash = _pairing_pair()
        headers = _fleet_headers(
            device_id="pi-x", pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
        )
        await unauthed_client.post(
            "/api/devices/register",
            json={"device_id": "pi-x", "pubkey": pub_b64,
                  "pairing_secret_hash": pairing_hash},
            headers=headers,
        )
        urlsafe = pub_b64.replace("+", "-").replace("/", "_").rstrip("=")
        resp = await unauthed_client.get(
            "/api/devices/bootstrap-status", params={"pubkey": urlsafe},
        )
        assert resp.status_code == 200
        assert resp.json()["status"] == "pending"


# ---------------------------------------------------------------------
# /adopt  (new bootstrap path; coexists with legacy /{id}/adopt)
# ---------------------------------------------------------------------


async def _seed_profile(db_session, name="test-profile"):
    from cms.models.device_profile import DeviceProfile
    profile = DeviceProfile(name=name)
    db_session.add(profile)
    await db_session.commit()
    return profile


class _StubTransport:
    """Minimal transport stub that only implements ``get_client_access_token``.

    Attach with ``cms.services.transport.set_transport`` for the duration
    of the test and restore afterwards.
    """

    async def get_client_access_token(self, user_id, minutes_to_expire=60):
        return {
            "url": f"wss://stub.example.com/client?user={user_id}",
            "token": f"stub-jwt-{user_id}-{minutes_to_expire}m",
        }


@pytest.fixture
def stub_wps_transport():
    from cms.services import transport as transport_mod
    original = transport_mod.get_transport()
    transport_mod.set_transport(_StubTransport())
    yield
    transport_mod.set_transport(original)


class TestAdopt:
    async def test_admin_adoption_creates_device_and_outbox(
        self, client, fleet_secret_enabled, stub_wps_transport,
        db_session, unauthed_client,
    ):
        priv, pub_b64 = _gen_keypair()
        pairing_secret, pairing_hash = _pairing_pair()
        headers = _fleet_headers(
            device_id="pi-x", pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
        )
        r = await unauthed_client.post(
            "/api/devices/register",
            json={"device_id": "pi-x", "pubkey": pub_b64,
                  "pairing_secret_hash": pairing_hash},
            headers=headers,
        )
        assert r.status_code == 202

        profile = await _seed_profile(db_session, name="adopt-prof")

        adopt_resp = await client.post(
            "/api/devices/adopt",
            json={
                "pairing_secret": pairing_secret,
                "name": "Lobby display",
                "location": "HQ lobby",
                "profile_id": str(profile.id),
            },
        )
        assert adopt_resp.status_code == 200, adopt_resp.text
        device_id = adopt_resp.json()["device_id"]

        from cms.models.device import Device, DeviceStatus
        from cms.models.pending_registration import PendingRegistration
        from sqlalchemy import select
        device = (
            await db_session.execute(
                select(Device).where(Device.id == device_id)
            )
        ).scalar_one()
        assert device.status == DeviceStatus.ADOPTED
        assert device.pubkey == pub_b64
        assert device.name == "Lobby display"

        pending = (
            await db_session.execute(
                select(PendingRegistration).where(
                    PendingRegistration.pairing_secret_hash == pairing_hash,
                )
            )
        ).scalar_one()
        assert pending.adopted_at is not None
        assert pending.adopted_device_id == device_id
        assert pending.outbox_ciphertext  # non-empty

        # Decrypt with the device's private key and validate the payload.
        priv_raw = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        plaintext = device_identity.decrypt_with_device_key(
            priv_raw, pending.outbox_ciphertext,
        )
        payload = json.loads(plaintext)
        assert payload["device_id"] == device_id
        assert payload["wps_url"].startswith("wss://stub.example.com/")
        assert payload["wps_jwt"].startswith("stub-jwt-")

    async def test_missing_auth_rejected(
        self, unauthed_client, fleet_secret_enabled, db_session,
    ):
        profile = await _seed_profile(db_session, name="noauth")
        r = await unauthed_client.post(
            "/api/devices/adopt",
            json={
                "pairing_secret": "nope",
                "profile_id": str(profile.id),
            },
        )
        assert r.status_code in (401, 403)

    async def test_unknown_pairing_secret_returns_404(
        self, client, fleet_secret_enabled, stub_wps_transport, db_session,
    ):
        profile = await _seed_profile(db_session, name="unknown-sec")
        r = await client.post(
            "/api/devices/adopt",
            json={
                "pairing_secret": "notasecret",
                "profile_id": str(profile.id),
            },
        )
        assert r.status_code == 404

    async def test_unknown_group_returns_404(
        self, client, fleet_secret_enabled, stub_wps_transport,
        db_session, unauthed_client,
    ):
        _, pub_b64 = _gen_keypair()
        pairing_secret, pairing_hash = _pairing_pair()
        headers = _fleet_headers(
            device_id="pi-x", pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
        )
        await unauthed_client.post(
            "/api/devices/register",
            json={"device_id": "pi-x", "pubkey": pub_b64,
                  "pairing_secret_hash": pairing_hash},
            headers=headers,
        )
        profile = await _seed_profile(db_session, name="noghgrp")
        r = await client.post(
            "/api/devices/adopt",
            json={
                "pairing_secret": pairing_secret,
                "profile_id": str(profile.id),
                "group_id": str(uuid.uuid4()),
            },
        )
        assert r.status_code == 404
        assert "group" in r.json()["detail"]

    async def test_already_adopted_returns_409(
        self, client, fleet_secret_enabled, stub_wps_transport,
        db_session, unauthed_client,
    ):
        _, pub_b64 = _gen_keypair()
        pairing_secret, pairing_hash = _pairing_pair()
        headers = _fleet_headers(
            device_id="pi-x", pubkey_b64=pub_b64,
            pairing_secret_hash_hex=pairing_hash,
        )
        await unauthed_client.post(
            "/api/devices/register",
            json={"device_id": "pi-x", "pubkey": pub_b64,
                  "pairing_secret_hash": pairing_hash},
            headers=headers,
        )
        profile = await _seed_profile(db_session, name="once")

        r1 = await client.post(
            "/api/devices/adopt",
            json={"pairing_secret": pairing_secret,
                  "profile_id": str(profile.id)},
        )
        assert r1.status_code == 200

        r2 = await client.post(
            "/api/devices/adopt",
            json={"pairing_secret": pairing_secret,
                  "profile_id": str(profile.id)},
        )
        assert r2.status_code == 409

    async def test_adoption_cleans_up_stale_pending_rows_for_same_device(
        self, client, fleet_secret_enabled, stub_wps_transport,
        db_session, unauthed_client,
    ):
        """Regression: when a Pi cycles through register attempts (e.g.
        before a factory reset or firmware upgrade), each fresh pairing
        secret creates a new pending_registrations row for the same
        device_id.  Adopting the latest one must clean up the stale
        un-adopted siblings; otherwise they linger until the 24h TTL
        and the operator sees the device on /devices Pending Devices
        even after a successful adopt.
        """
        from cms.models.pending_registration import PendingRegistration
        from sqlalchemy import select

        # First register: stale row, never adopted (e.g. pre-reset).
        _, _, stale_hash, _ = await _register_one(
            unauthed_client, device_id="pi-recycled",
        )

        # Second register: different keypair + secret, same device_id
        # (e.g. post-factory-reset).  Creates a second pending row.
        priv2, pub_b64_2 = _gen_keypair()
        secret2, hash2 = _pairing_pair()
        headers2 = _fleet_headers(
            device_id="pi-recycled", pubkey_b64=pub_b64_2,
            pairing_secret_hash_hex=hash2,
        )
        r = await unauthed_client.post(
            "/api/devices/register",
            json={"device_id": "pi-recycled", "pubkey": pub_b64_2,
                  "pairing_secret_hash": hash2},
            headers=headers2,
        )
        assert r.status_code == 202

        # Sanity: both rows exist, both un-adopted.
        rows = (
            await db_session.execute(
                select(PendingRegistration).where(
                    PendingRegistration.device_id == "pi-recycled",
                )
            )
        ).scalars().all()
        assert len(rows) == 2
        assert all(r.adopted_at is None for r in rows)

        # Adopt the second (current) one.
        profile = await _seed_profile(db_session, name="cleanup")
        adopt_resp = await client.post(
            "/api/devices/adopt",
            json={"pairing_secret": secret2,
                  "profile_id": str(profile.id)},
        )
        assert adopt_resp.status_code == 200, adopt_resp.text

        # Stale row must be gone; adopted row must remain with
        # adopted_at + outbox payload intact.
        db_session.expire_all()
        rows = (
            await db_session.execute(
                select(PendingRegistration).where(
                    PendingRegistration.device_id == "pi-recycled",
                )
            )
        ).scalars().all()
        assert len(rows) == 1, (
            f"expected stale un-adopted row to be deleted; got "
            f"{[(str(r.id), r.adopted_at) for r in rows]}"
        )
        assert rows[0].pairing_secret_hash == hash2
        assert rows[0].adopted_at is not None
        assert rows[0].outbox_ciphertext

        # And /pending must not list the device any more.
        listed = await client.get("/api/devices/pending")
        assert listed.status_code == 200
        assert listed.json() == {"items": []}


# ---------------------------------------------------------------------
# /pending + /adopt-pending + DELETE /pending/{id}
# ---------------------------------------------------------------------


async def _register_one(
    unauthed_client, device_id: str = "pi-x",
) -> tuple[str, str, str, Ed25519PrivateKey]:
    """Helper: call /register once and return (device_id, pairing_secret,
    pairing_hash, priv)."""
    priv, pub_b64 = _gen_keypair()
    pairing_secret, pairing_hash = _pairing_pair()
    headers = _fleet_headers(
        device_id=device_id, pubkey_b64=pub_b64,
        pairing_secret_hash_hex=pairing_hash,
    )
    r = await unauthed_client.post(
        "/api/devices/register",
        json={"device_id": device_id, "pubkey": pub_b64,
              "pairing_secret_hash": pairing_hash},
        headers=headers,
    )
    assert r.status_code == 202, r.text
    return device_id, pairing_secret, pairing_hash, priv


class TestListPending:
    async def test_requires_auth(
        self, unauthed_client, fleet_secret_enabled,
    ):
        r = await unauthed_client.get("/api/devices/pending")
        assert r.status_code in (401, 403)

    async def test_empty_list(self, client, fleet_secret_enabled):
        r = await client.get("/api/devices/pending")
        assert r.status_code == 200
        assert r.json() == {"items": []}

    async def test_lists_registered_device(
        self, client, unauthed_client, fleet_secret_enabled,
    ):
        await _register_one(unauthed_client, device_id="pi-alpha")
        r = await client.get("/api/devices/pending")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        row = items[0]
        assert row["device_id"] == "pi-alpha"
        assert isinstance(row["id"], str)
        uuid.UUID(row["id"])  # must parse as uuid
        assert row["pubkey"]
        # pairing_secret_hash and outbox_ciphertext MUST NOT leak
        assert "pairing_secret_hash" not in row
        assert "outbox_ciphertext" not in row
        assert "pairing_secret" not in row

    async def test_newest_first(
        self, client, unauthed_client, fleet_secret_enabled,
    ):
        await _register_one(unauthed_client, device_id="pi-first")
        await _register_one(unauthed_client, device_id="pi-second")
        r = await client.get("/api/devices/pending")
        items = r.json()["items"]
        assert [i["device_id"] for i in items] == ["pi-second", "pi-first"]

    async def test_adopted_rows_excluded(
        self, client, unauthed_client, fleet_secret_enabled,
        stub_wps_transport, db_session,
    ):
        _, secret_a, _, _ = await _register_one(
            unauthed_client, device_id="pi-adopted",
        )
        await _register_one(unauthed_client, device_id="pi-still-pending")
        profile = await _seed_profile(db_session, name="excl")
        adopt = await client.post(
            "/api/devices/adopt",
            json={"pairing_secret": secret_a,
                  "profile_id": str(profile.id)},
        )
        assert adopt.status_code == 200
        r = await client.get("/api/devices/pending")
        items = r.json()["items"]
        assert [i["device_id"] for i in items] == ["pi-still-pending"]


class TestAdoptPending:
    async def test_admin_adoption_by_pending_id(
        self, client, unauthed_client, fleet_secret_enabled,
        stub_wps_transport, db_session,
    ):
        _, _, pairing_hash, priv = await _register_one(
            unauthed_client, device_id="pi-by-id",
        )
        # Discover the pending_id via the list endpoint (admin-only).
        listing = await client.get("/api/devices/pending")
        pending_id = listing.json()["items"][0]["id"]

        profile = await _seed_profile(db_session, name="byid-prof")
        r = await client.post(
            "/api/devices/adopt-pending",
            json={
                "pending_id": pending_id,
                "name": "Lobby",
                "profile_id": str(profile.id),
            },
        )
        assert r.status_code == 200, r.text
        device_id = r.json()["device_id"]

        from cms.models.device import Device, DeviceStatus
        from cms.models.pending_registration import PendingRegistration
        from sqlalchemy import select

        device = (
            await db_session.execute(
                select(Device).where(Device.id == device_id)
            )
        ).scalar_one()
        assert device.status == DeviceStatus.ADOPTED
        assert device.name == "Lobby"

        pending = (
            await db_session.execute(
                select(PendingRegistration).where(
                    PendingRegistration.pairing_secret_hash == pairing_hash,
                )
            )
        ).scalar_one()
        assert pending.adopted_at is not None
        assert pending.outbox_ciphertext
        # Device can still decrypt the outbox with its private key.
        priv_raw = priv.private_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PrivateFormat.Raw,
            encryption_algorithm=serialization.NoEncryption(),
        )
        plaintext = device_identity.decrypt_with_device_key(
            priv_raw, pending.outbox_ciphertext,
        )
        payload = json.loads(plaintext)
        assert payload["device_id"] == device_id

    async def test_missing_auth_rejected(
        self, unauthed_client, fleet_secret_enabled, db_session,
    ):
        profile = await _seed_profile(db_session, name="noauth-byid")
        r = await unauthed_client.post(
            "/api/devices/adopt-pending",
            json={
                "pending_id": str(uuid.uuid4()),
                "profile_id": str(profile.id),
            },
        )
        assert r.status_code in (401, 403)

    async def test_unknown_pending_id_returns_404(
        self, client, fleet_secret_enabled, stub_wps_transport, db_session,
    ):
        profile = await _seed_profile(db_session, name="unknown-byid")
        r = await client.post(
            "/api/devices/adopt-pending",
            json={
                "pending_id": str(uuid.uuid4()),
                "profile_id": str(profile.id),
            },
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "pending_not_found"

    async def test_malformed_uuid_returns_404(
        self, client, fleet_secret_enabled, stub_wps_transport, db_session,
    ):
        profile = await _seed_profile(db_session, name="badid")
        r = await client.post(
            "/api/devices/adopt-pending",
            json={
                "pending_id": "not-a-uuid",
                "profile_id": str(profile.id),
            },
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "pending_not_found"

    async def test_already_adopted_returns_409(
        self, client, unauthed_client, fleet_secret_enabled,
        stub_wps_transport, db_session,
    ):
        await _register_one(unauthed_client, device_id="pi-twice")
        listing = await client.get("/api/devices/pending")
        pending_id = listing.json()["items"][0]["id"]
        profile = await _seed_profile(db_session, name="twice")
        r1 = await client.post(
            "/api/devices/adopt-pending",
            json={"pending_id": pending_id,
                  "profile_id": str(profile.id)},
        )
        assert r1.status_code == 200
        r2 = await client.post(
            "/api/devices/adopt-pending",
            json={"pending_id": pending_id,
                  "profile_id": str(profile.id)},
        )
        assert r2.status_code == 409

    async def test_unknown_profile_returns_404(
        self, client, unauthed_client, fleet_secret_enabled,
        stub_wps_transport,
    ):
        await _register_one(unauthed_client, device_id="pi-noprof")
        listing = await client.get("/api/devices/pending")
        pending_id = listing.json()["items"][0]["id"]
        r = await client.post(
            "/api/devices/adopt-pending",
            json={"pending_id": pending_id,
                  "profile_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404
        assert r.json()["detail"] == "profile_not_found"


class TestRejectPending:
    async def test_requires_auth(
        self, unauthed_client, fleet_secret_enabled,
    ):
        r = await unauthed_client.delete(
            f"/api/devices/pending/{uuid.uuid4()}"
        )
        assert r.status_code in (401, 403)

    async def test_delete_removes_row(
        self, client, unauthed_client, fleet_secret_enabled, db_session,
    ):
        await _register_one(unauthed_client, device_id="pi-rm")
        listing = await client.get("/api/devices/pending")
        pending_id = listing.json()["items"][0]["id"]
        r = await client.delete(f"/api/devices/pending/{pending_id}")
        assert r.status_code == 204
        listing2 = await client.get("/api/devices/pending")
        assert listing2.json()["items"] == []

    async def test_unknown_id_returns_404(
        self, client, fleet_secret_enabled,
    ):
        r = await client.delete(f"/api/devices/pending/{uuid.uuid4()}")
        assert r.status_code == 404

    async def test_malformed_uuid_returns_404(
        self, client, fleet_secret_enabled,
    ):
        r = await client.delete("/api/devices/pending/not-a-uuid")
        assert r.status_code == 404

    async def test_already_adopted_cannot_be_rejected(
        self, client, unauthed_client, fleet_secret_enabled,
        stub_wps_transport, db_session,
    ):
        _, pairing_secret, _, _ = await _register_one(
            unauthed_client, device_id="pi-adopted-rm",
        )
        listing = await client.get("/api/devices/pending")
        pending_id = listing.json()["items"][0]["id"]
        profile = await _seed_profile(db_session, name="adopted-rm")
        a = await client.post(
            "/api/devices/adopt",
            json={"pairing_secret": pairing_secret,
                  "profile_id": str(profile.id)},
        )
        assert a.status_code == 200
        r = await client.delete(f"/api/devices/pending/{pending_id}")
        assert r.status_code == 404

    async def test_audit_row_uses_device_id(
        self, client, unauthed_client, fleet_secret_enabled, db_session,
    ):
        """Reject of a pending row writes an audit entry that names
        the physical device by its serial in description, resource_id,
        and details — not the disposable pending UUID alone.
        """
        from sqlalchemy import select

        from cms.models.audit_log import AuditLog

        await _register_one(unauthed_client, device_id="pi-audit-name")
        listing = await client.get("/api/devices/pending")
        pending_id = listing.json()["items"][0]["id"]

        r = await client.delete(f"/api/devices/pending/{pending_id}")
        assert r.status_code == 204

        result = await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "device.pending.reject")
            .order_by(AuditLog.created_at.desc())
        )
        entry = result.scalars().first()
        assert entry is not None, "no audit row written for reject"
        assert entry.resource_type == "device"
        assert entry.resource_id == "pi-audit-name"
        assert "pi-audit-name" in (entry.description or "")
        assert entry.details["pending_id"] == pending_id
        assert entry.details["device_id"] == "pi-audit-name"


# ---------------------------------------------------------------------
# /connect-token
# ---------------------------------------------------------------------


async def _make_adopted_device(
    db_session, pub_b64: str, device_id: str | None = None,
):
    from cms.models.device import Device, DeviceStatus
    device_id = device_id or str(uuid.uuid4())
    device = Device(
        id=device_id, name="ct-dev", status=DeviceStatus.ADOPTED,
        pubkey=pub_b64,
    )
    db_session.add(device)
    await db_session.commit()
    return device


def _sign_connect_token(priv, device_id, timestamp, nonce):
    msg = device_identity.connect_token_canonical_bytes(
        device_id, str(timestamp), nonce,
    )
    sig = priv.sign(msg)
    return base64.b64encode(sig).decode("ascii")


class TestConnectToken:
    async def test_valid_signature_returns_jwt(
        self, unauthed_client, stub_wps_transport, db_session,
    ):
        priv, pub_b64 = _gen_keypair()
        device = await _make_adopted_device(db_session, pub_b64)
        ts = int(time.time())
        nonce = uuid.uuid4().hex
        sig = _sign_connect_token(priv, device.id, ts, nonce)
        r = await unauthed_client.post(
            "/api/devices/connect-token",
            json={
                "device_id": device.id, "timestamp": ts,
                "nonce": nonce, "signature": sig,
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["wps_jwt"].startswith("stub-jwt-")
        assert body["wps_url"]

    async def test_unknown_device_returns_401(
        self, unauthed_client, stub_wps_transport, db_session,
    ):
        priv, _ = _gen_keypair()
        ts = int(time.time())
        nonce = uuid.uuid4().hex
        sig = _sign_connect_token(priv, "ghost", ts, nonce)
        r = await unauthed_client.post(
            "/api/devices/connect-token",
            json={"device_id": "ghost", "timestamp": ts,
                  "nonce": nonce, "signature": sig},
        )
        assert r.status_code == 401

    async def test_revoked_pubkey_returns_401(
        self, unauthed_client, stub_wps_transport, db_session,
    ):
        priv, pub_b64 = _gen_keypair()
        device = await _make_adopted_device(db_session, pub_b64)
        # Revoke.
        device.pubkey = None
        await db_session.commit()
        ts = int(time.time())
        nonce = uuid.uuid4().hex
        sig = _sign_connect_token(priv, device.id, ts, nonce)
        r = await unauthed_client.post(
            "/api/devices/connect-token",
            json={"device_id": device.id, "timestamp": ts,
                  "nonce": nonce, "signature": sig},
        )
        assert r.status_code == 401

    async def test_tampered_signature_returns_401(
        self, unauthed_client, stub_wps_transport, db_session,
    ):
        priv, pub_b64 = _gen_keypair()
        device = await _make_adopted_device(db_session, pub_b64)
        ts = int(time.time())
        nonce = uuid.uuid4().hex
        sig = _sign_connect_token(priv, device.id, ts, nonce)
        # Flip a byte.
        raw = bytearray(base64.b64decode(sig))
        raw[0] ^= 0x01
        bad_sig = base64.b64encode(bytes(raw)).decode("ascii")
        r = await unauthed_client.post(
            "/api/devices/connect-token",
            json={"device_id": device.id, "timestamp": ts,
                  "nonce": nonce, "signature": bad_sig},
        )
        assert r.status_code == 401

    async def test_stale_timestamp_returns_401(
        self, unauthed_client, stub_wps_transport, db_session,
    ):
        priv, pub_b64 = _gen_keypair()
        device = await _make_adopted_device(db_session, pub_b64)
        ts = int(time.time()) - 3600
        nonce = uuid.uuid4().hex
        sig = _sign_connect_token(priv, device.id, ts, nonce)
        r = await unauthed_client.post(
            "/api/devices/connect-token",
            json={"device_id": device.id, "timestamp": ts,
                  "nonce": nonce, "signature": sig},
        )
        assert r.status_code == 401

    async def test_replay_returns_401(
        self, unauthed_client, stub_wps_transport, db_session,
    ):
        priv, pub_b64 = _gen_keypair()
        device = await _make_adopted_device(db_session, pub_b64)
        ts = int(time.time())
        nonce = uuid.uuid4().hex
        sig = _sign_connect_token(priv, device.id, ts, nonce)
        body = {"device_id": device.id, "timestamp": ts,
                "nonce": nonce, "signature": sig}
        r1 = await unauthed_client.post("/api/devices/connect-token", json=body)
        assert r1.status_code == 200
        r2 = await unauthed_client.post("/api/devices/connect-token", json=body)
        assert r2.status_code == 401

    async def test_non_adopted_status_returns_401(
        self, unauthed_client, stub_wps_transport, db_session,
    ):
        """A Device row that exists and still has its pubkey but is not
        in ``ADOPTED`` state (e.g. PENDING, or left behind by a removal
        flow that nulled status but not pubkey) must not be able to mint
        fresh WPS JWTs.  Prevents bypass of admin revocation that only
        changed ``status`` without clearing ``pubkey``.
        """
        from cms.models.device import Device, DeviceStatus

        priv, pub_b64 = _gen_keypair()
        device = Device(
            id=str(uuid.uuid4()), name="ct-pending",
            status=DeviceStatus.PENDING, pubkey=pub_b64,
        )
        db_session.add(device)
        await db_session.commit()

        ts = int(time.time())
        nonce = uuid.uuid4().hex
        sig = _sign_connect_token(priv, device.id, ts, nonce)
        r = await unauthed_client.post(
            "/api/devices/connect-token",
            json={"device_id": device.id, "timestamp": ts,
                  "nonce": nonce, "signature": sig},
        )
        assert r.status_code == 401


# ---------------------------------------------------------------------
# /register — pubkey-hijack defence
# ---------------------------------------------------------------------


class TestRegisterPubkeyHijack:
    async def test_same_pairing_hash_different_pubkey_returns_409(
        self, unauthed_client, fleet_secret_enabled, db_session,
    ):
        """Attacker (with the fleet HMAC secret + the leaked pairing
        secret) must not be able to overwrite an existing unadopted
        pending row's pubkey with their own.  Doing so would cause the
        admin's adopt-time ECIES-encrypted payload to be decryptable by
        the attacker instead of the real device.
        """
        _, pub_legit = _gen_keypair()
        _, pub_attacker = _gen_keypair()
        _, pairing_hash = _pairing_pair()

        body1 = {
            "device_id": "pi-legit", "pubkey": pub_legit,
            "pairing_secret_hash": pairing_hash,
        }
        r1 = await unauthed_client.post(
            "/api/devices/register", json=body1,
            headers=_fleet_headers(
                device_id=body1["device_id"], pubkey_b64=pub_legit,
                pairing_secret_hash_hex=pairing_hash,
            ),
        )
        assert r1.status_code == 202

        body2 = {
            "device_id": "pi-attacker", "pubkey": pub_attacker,
            "pairing_secret_hash": pairing_hash,
        }
        r2 = await unauthed_client.post(
            "/api/devices/register", json=body2,
            headers=_fleet_headers(
                device_id=body2["device_id"], pubkey_b64=pub_attacker,
                pairing_secret_hash_hex=pairing_hash,
            ),
        )
        assert r2.status_code == 409
        assert r2.json()["detail"] == "pubkey_mismatch"

        # DB still shows the legitimate pubkey untouched.
        from cms.models.pending_registration import PendingRegistration
        from sqlalchemy import select
        row = (
            await db_session.execute(
                select(PendingRegistration).where(
                    PendingRegistration.pairing_secret_hash == pairing_hash,
                )
            )
        ).scalar_one()
        assert row.pubkey == pub_legit
        assert row.device_id == "pi-legit"


# ---------------------------------------------------------------------
# pending_registrations TTL reaper
# ---------------------------------------------------------------------


class TestPendingRegistrationsReaper:
    async def test_unpolled_expired_row_deleted(self, db_session):
        """Rows that were created, HMAC-authed, but never polled get
        dropped once the unpolled TTL elapses.  Main defence against
        registration spam burning cap slots.
        """
        import uuid as _uuid
        from datetime import datetime, timedelta, timezone
        from cms.models.pending_registration import PendingRegistration
        from cms.services.device_bootstrap import reap_pending_registrations
        from sqlalchemy import select, func

        stale_ts = datetime.now(timezone.utc) - timedelta(hours=2)
        db_session.add(PendingRegistration(
            id=_uuid.uuid4(), device_id="old", pubkey="stale-pubkey",
            pairing_secret_hash="a" * 64,
            created_at=stale_ts, updated_at=stale_ts,
        ))
        db_session.add(PendingRegistration(
            id=_uuid.uuid4(), device_id="fresh", pubkey="fresh-pubkey",
            pairing_secret_hash="b" * 64,
        ))
        await db_session.commit()

        deleted = await reap_pending_registrations(
            db=db_session,
            unpolled_ttl_seconds=3600,
            polled_ttl_seconds=86_400,
            adopted_ttl_seconds=86_400,
        )
        assert deleted == 1

        remaining = (
            await db_session.execute(
                select(func.count()).select_from(PendingRegistration)
            )
        ).scalar_one()
        assert remaining == 1

    async def test_polled_unadopted_uses_polled_ttl(self, db_session):
        """A row the device has polled survives past the unpolled TTL
        so admins have time to finish the adoption flow — but is still
        eventually reaped if adoption never happens.
        """
        import uuid as _uuid
        from datetime import datetime, timedelta, timezone
        from cms.models.pending_registration import PendingRegistration
        from cms.services.device_bootstrap import reap_pending_registrations
        from sqlalchemy import select, func

        # Polled 2h ago — past unpolled TTL (1h) but within polled TTL (24h).
        polled_ts = datetime.now(timezone.utc) - timedelta(hours=2)
        db_session.add(PendingRegistration(
            id=_uuid.uuid4(), device_id="polled-recent",
            pubkey="pk-recent", pairing_secret_hash="c" * 64,
            polled_at=polled_ts,
        ))
        # Polled 30h ago — past polled TTL.
        polled_old_ts = datetime.now(timezone.utc) - timedelta(hours=30)
        db_session.add(PendingRegistration(
            id=_uuid.uuid4(), device_id="polled-old",
            pubkey="pk-old", pairing_secret_hash="d" * 64,
            polled_at=polled_old_ts,
        ))
        await db_session.commit()

        deleted = await reap_pending_registrations(
            db=db_session,
            unpolled_ttl_seconds=3600,
            polled_ttl_seconds=86_400,
            adopted_ttl_seconds=86_400,
        )
        assert deleted == 1

        rows = (
            await db_session.execute(select(PendingRegistration))
        ).scalars().all()
        assert len(rows) == 1
        assert rows[0].device_id == "polled-recent"

    async def test_adopted_expired_row_deleted(self, db_session):
        """Adopted rows are kept briefly for troubleshooting then
        dropped.  Bounds long-term table growth for adopted devices too.
        """
        import uuid as _uuid
        from datetime import datetime, timedelta, timezone
        from cms.models.pending_registration import PendingRegistration
        from cms.services.device_bootstrap import reap_pending_registrations
        from sqlalchemy import select, func

        adopted_ts = datetime.now(timezone.utc) - timedelta(hours=48)
        db_session.add(PendingRegistration(
            id=_uuid.uuid4(), device_id="ancient-adopted",
            pubkey="pk-adopted", pairing_secret_hash="e" * 64,
            adopted_at=adopted_ts,
        ))
        await db_session.commit()

        deleted = await reap_pending_registrations(
            db=db_session,
            unpolled_ttl_seconds=3600,
            polled_ttl_seconds=86_400,
            adopted_ttl_seconds=86_400,
        )
        assert deleted == 1

