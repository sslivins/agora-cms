"""Tests for the Stage 3b async log-request API (#345).

Covers:

* POST /api/logs/requests — dispatches when device is connected, falls
  back to pending when offline, 403 on group-access violation
* GET  /api/logs/requests/{id} — status payload + download_url
* GET  /api/logs/requests/{id}/download — 409 when not ready, blob
  bytes when ready
* POST /api/devices/{id}/logs/{rid}/upload — writes the blob, flips
  the outbox row to ready; 413 on oversize; 404 on unknown rid

Avoids the cross-loop asyncpg bug by using the ``app`` + ``client``
fixtures (httpx AsyncClient on the same loop as pytest-asyncio) and
seeding the DB through the app's session factory.
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select

from cms.database import get_session_factory
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.log_request import (
    STATUS_PENDING,
    STATUS_READY,
    STATUS_SENT,
    LogRequest,
)
from cms.models.user import Role, User, UserGroup
from cms.services import log_outbox


# ── Fake transport ───────────────────────────────────────────────────

class FakeTransport:
    """Tracks dispatch_request_logs calls and returns success/failure
    based on whether the device is flagged as connected."""

    def __init__(self):
        self.connected: set[str] = set()
        self.dispatched: list[dict] = []

    async def dispatch_request_logs(self, device_id, *, request_id, services=None, since="24h"):
        self.dispatched.append(
            {"device_id": device_id, "request_id": request_id,
             "services": services, "since": since},
        )
        if device_id not in self.connected:
            raise ValueError(f"Device {device_id} is not connected")

    # Other DeviceTransport methods aren't used by the router, but we
    # give them minimal stubs in case upstream calls them.
    async def send_to_device(self, device_id, message):
        return device_id in self.connected

    async def is_connected(self, device_id):
        return device_id in self.connected

    async def connected_count(self):
        return len(self.connected)

    async def connected_ids(self):
        return list(self.connected)

    async def get_all_states(self):
        return []

    async def set_state_flags(self, device_id, **flags):
        pass


@pytest_asyncio.fixture
async def fake_transport(app):
    from cms.services import transport as transport_module
    original = transport_module._transport
    fake = FakeTransport()
    transport_module._transport = fake
    try:
        yield fake
    finally:
        transport_module._transport = original


# ── Seed helpers ────────────────────────────────────────────────────

DEVICE_ID = "dev-logreq-1"
DEVICE_API_KEY = "key-logreq-1"


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


@pytest_asyncio.fixture
async def seeded(app):
    """Seed a group, device (with API key) and a non-admin user in the
    admin group.  Returns a dict of ids."""
    factory = get_session_factory()
    group_id = uuid.uuid4()
    async with factory() as db:
        group = DeviceGroup(id=group_id, name="LogReq Group")
        db.add(group)
        device = Device(
            id=DEVICE_ID,
            name="LogReq Device",
            status=DeviceStatus.ADOPTED,
            group_id=group_id,
            device_api_key_hash=_hash_key(DEVICE_API_KEY),
        )
        db.add(device)
        await db.commit()
    return {"group_id": group_id, "device_id": DEVICE_ID}


@pytest_asyncio.fixture
async def other_group(app):
    """A second group the admin user is NOT scoped to (for 403 tests)."""
    factory = get_session_factory()
    gid = uuid.uuid4()
    async with factory() as db:
        db.add(DeviceGroup(id=gid, name="Other Group"))
        await db.commit()
    return gid


async def _new_viewer_client(app, *, group_ids):
    """Create a Viewer user in ``group_ids`` and return a logged-in client."""
    factory = get_session_factory()
    from cms.auth import hash_password

    username = f"viewer-{uuid.uuid4().hex[:8]}"
    async with factory() as db:
        role = (
            await db.execute(select(Role).where(Role.name == "Viewer"))
        ).scalar_one()
        user = User(
            username=username,
            email=f"{username}@t",
            display_name="Viewer",
            password_hash=hash_password("testpass"),
            role_id=role.id,
            is_active=True,
            must_change_password=False,
        )
        db.add(user)
        await db.flush()
        for gid in group_ids:
            db.add(UserGroup(user_id=user.id, group_id=gid))
        await db.commit()

    transport = ASGITransport(app=app)
    ac = AsyncClient(transport=transport, base_url="http://test")
    await ac.post("/login", data={"username": username, "password": "testpass"})
    return ac


# ── POST /api/logs/requests ─────────────────────────────────────────

class TestCreateLogRequest:
    @pytest.mark.asyncio
    async def test_connected_device_dispatches_and_marks_sent(
        self, client, seeded, fake_transport,
    ):
        fake_transport.connected.add(DEVICE_ID)

        resp = await client.post(
            "/api/logs/requests",
            json={"device_id": DEVICE_ID, "since": "1h"},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "sent"
        assert body["request_id"]
        assert len(fake_transport.dispatched) == 1

        # Verify row exists + status in DB
        factory = get_session_factory()
        async with factory() as db:
            row = await log_outbox.get(db, body["request_id"])
            assert row is not None
            assert row.status == STATUS_SENT
            assert row.attempts == 1

    @pytest.mark.asyncio
    async def test_offline_device_still_returns_202_pending(
        self, client, seeded, fake_transport,
    ):
        # fake_transport.connected is empty → dispatch raises ValueError
        resp = await client.post(
            "/api/logs/requests",
            json={"device_id": DEVICE_ID, "services": ["agora-player"]},
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["status"] == "pending"

        factory = get_session_factory()
        async with factory() as db:
            row = await log_outbox.get(db, body["request_id"])
            assert row.status == STATUS_PENDING
            assert row.attempts == 1  # record_attempt_error bumped it
            assert "not connected" in (row.last_error or "")

    @pytest.mark.asyncio
    async def test_group_access_denied(
        self, app, seeded, other_group, fake_transport,
    ):
        viewer = await _new_viewer_client(app, group_ids=[other_group])
        try:
            resp = await viewer.post(
                "/api/logs/requests",
                json={"device_id": DEVICE_ID},
            )
            assert resp.status_code == 403
        finally:
            await viewer.aclose()


# ── GET /api/logs/requests/{id} ─────────────────────────────────────

class TestGetLogRequest:
    @pytest.mark.asyncio
    async def test_pending_row_no_download_url(self, client, seeded):
        factory = get_session_factory()
        async with factory() as db:
            row = await log_outbox.create(db, device_id=DEVICE_ID)
            await db.commit()
            rid = row.id

        resp = await client.get(f"/api/logs/requests/{rid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["request_id"] == rid
        assert body["status"] == STATUS_PENDING
        assert body["download_url"] is None

    @pytest.mark.asyncio
    async def test_ready_row_has_download_url(self, client, seeded):
        factory = get_session_factory()
        async with factory() as db:
            row = await log_outbox.create(db, device_id=DEVICE_ID)
            await db.commit()
            await log_outbox.mark_ready(
                db, row.id,
                blob_path=f"{DEVICE_ID}/{row.id}.tar.gz", size_bytes=42,
            )
            await db.commit()
            rid = row.id

        resp = await client.get(f"/api/logs/requests/{rid}")
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == STATUS_READY
        assert body["download_url"] == f"/api/logs/requests/{rid}/download"
        assert body["size_bytes"] == 42

    @pytest.mark.asyncio
    async def test_unknown_id_is_404(self, client, seeded):
        resp = await client.get("/api/logs/requests/does-not-exist")
        assert resp.status_code == 404


# ── GET /api/logs/requests/{id}/download ─────────────────────────────

class TestDownloadLogRequest:
    @pytest.mark.asyncio
    async def test_returns_blob_bytes_when_ready(self, app, client, seeded, tmp_path):
        # Write a fake blob into the LocalLogBlobBackend that the app
        # fixture set up (under <asset_storage_path>/device-logs/).
        from cms.services.log_blob import write_log_blob, init_log_storage, set_log_backend, LocalLogBlobBackend
        # The `app` fixture initialises asset storage but not log
        # storage — wire a backend up against the same tmp path.
        from cms.auth import get_settings
        settings = app.dependency_overrides[get_settings]()
        set_log_backend(LocalLogBlobBackend(base_path=settings.asset_storage_path))
        # Ensure the base dir exists.
        await init_log_storage(settings)

        factory = get_session_factory()
        async with factory() as db:
            row = await log_outbox.create(db, device_id=DEVICE_ID)
            await db.commit()
            rid = row.id

        blob_path = f"{DEVICE_ID}/{rid}.tar.gz"
        payload = b"hello-tarball"
        size = await write_log_blob(blob_path, payload)
        assert size == len(payload)

        async with factory() as db:
            await log_outbox.mark_ready(
                db, rid, blob_path=blob_path, size_bytes=size,
            )
            await db.commit()

        resp = await client.get(f"/api/logs/requests/{rid}/download")
        assert resp.status_code == 200
        assert resp.content == payload
        assert "attachment" in resp.headers.get("content-disposition", "")

    @pytest.mark.asyncio
    async def test_successful_download_marks_row_for_expiry(
        self, app, client, seeded, tmp_path,
    ):
        # Once the user has pulled the bundle we don't want to keep the
        # blob around — the download handler should bump ``expires_at``
        # to now so the next reaper tick cleans it up.
        from datetime import datetime, timezone
        from cms.services.log_blob import (
            LocalLogBlobBackend,
            init_log_storage,
            set_log_backend,
            write_log_blob,
        )
        from cms.auth import get_settings

        settings = app.dependency_overrides[get_settings]()
        set_log_backend(LocalLogBlobBackend(base_path=settings.asset_storage_path))
        await init_log_storage(settings)

        factory = get_session_factory()
        async with factory() as db:
            row = await log_outbox.create(db, device_id=DEVICE_ID)
            await db.commit()
            rid = row.id
            original_expires_at = row.expires_at

        blob_path = f"{DEVICE_ID}/{rid}.tar.gz"
        payload = b"hello-tarball"
        await write_log_blob(blob_path, payload)
        async with factory() as db:
            await log_outbox.mark_ready(
                db, rid, blob_path=blob_path, size_bytes=len(payload),
            )
            await db.commit()

        before = datetime.now(timezone.utc)
        resp = await client.get(f"/api/logs/requests/{rid}/download")
        assert resp.status_code == 200

        async with factory() as db:
            refreshed = await log_outbox.get(db, rid)
            # Row stays ``ready`` with blob_path intact so the reaper
            # can find the blob; only expires_at moves to "now".
            assert refreshed.status == STATUS_READY
            assert refreshed.blob_path == blob_path
            assert refreshed.expires_at is not None
            # SQLite strips tz info; normalise for comparison.
            expires = refreshed.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            original = original_expires_at
            if original.tzinfo is None:
                original = original.replace(tzinfo=timezone.utc)
            assert expires <= datetime.now(timezone.utc)
            # And it was genuinely moved forward — not just the
            # original 1 h TTL.
            assert expires < original
            assert expires >= before

    @pytest.mark.asyncio
    async def test_pending_row_returns_409(self, client, seeded):
        factory = get_session_factory()
        async with factory() as db:
            row = await log_outbox.create(db, device_id=DEVICE_ID)
            await db.commit()
            rid = row.id

        resp = await client.get(f"/api/logs/requests/{rid}/download")
        assert resp.status_code == 409

    @pytest.mark.asyncio
    async def test_unknown_id_is_404(self, client, seeded):
        resp = await client.get("/api/logs/requests/no-such-id/download")
        assert resp.status_code == 404


# ── Upload endpoint ─────────────────────────────────────────────────

class TestUploadLogBundle:
    @pytest.mark.asyncio
    async def test_happy_path_writes_blob_and_marks_ready(
        self, app, unauthed_client, seeded,
    ):
        from cms.services.log_blob import (
            LocalLogBlobBackend, init_log_storage, set_log_backend,
        )
        from cms.auth import get_settings

        settings = app.dependency_overrides[get_settings]()
        set_log_backend(LocalLogBlobBackend(base_path=settings.asset_storage_path))
        await init_log_storage(settings)

        factory = get_session_factory()
        async with factory() as db:
            row = await log_outbox.create(db, device_id=DEVICE_ID)
            await db.commit()
            rid = row.id

        payload = b"fake-tar-content" * 32  # 512 bytes

        resp = await unauthed_client.post(
            f"/api/devices/{DEVICE_ID}/logs/{rid}/upload",
            content=payload,
            headers={"X-Device-API-Key": DEVICE_API_KEY},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ready"
        assert body["size_bytes"] == len(payload)

        # Verify blob and row
        blob_file = (
            Path(settings.asset_storage_path)
            / "device-logs" / DEVICE_ID / f"{rid}.tar.gz"
        )
        assert blob_file.is_file()
        assert blob_file.read_bytes() == payload

        async with factory() as db:
            row = await log_outbox.get(db, rid)
            assert row.status == STATUS_READY
            assert row.size_bytes == len(payload)

    @pytest.mark.asyncio
    async def test_oversize_rejected_with_413(
        self, app, unauthed_client, seeded,
    ):
        from cms.routers import log_requests as log_requests_mod
        from cms.services.log_blob import (
            LocalLogBlobBackend, init_log_storage, set_log_backend,
        )
        from cms.auth import get_settings

        settings = app.dependency_overrides[get_settings]()
        set_log_backend(LocalLogBlobBackend(base_path=settings.asset_storage_path))
        await init_log_storage(settings)

        # Patch the module-level cap so the test doesn't have to push
        # 100 MB through the ASGI transport.
        original = log_requests_mod.MAX_UPLOAD_BYTES
        log_requests_mod.MAX_UPLOAD_BYTES = 100  # tiny cap for test
        try:
            factory = get_session_factory()
            async with factory() as db:
                row = await log_outbox.create(db, device_id=DEVICE_ID)
                await db.commit()
                rid = row.id

            resp = await unauthed_client.post(
                f"/api/devices/{DEVICE_ID}/logs/{rid}/upload",
                content=b"x" * 500,
                headers={"X-Device-API-Key": DEVICE_API_KEY},
            )
            assert resp.status_code == 413
        finally:
            log_requests_mod.MAX_UPLOAD_BYTES = original

    @pytest.mark.asyncio
    async def test_unknown_request_id_is_404(
        self, app, unauthed_client, seeded,
    ):
        from cms.services.log_blob import (
            LocalLogBlobBackend, init_log_storage, set_log_backend,
        )
        from cms.auth import get_settings
        settings = app.dependency_overrides[get_settings]()
        set_log_backend(LocalLogBlobBackend(base_path=settings.asset_storage_path))
        await init_log_storage(settings)

        resp = await unauthed_client.post(
            f"/api/devices/{DEVICE_ID}/logs/no-such-rid/upload",
            content=b"x",
            headers={"X-Device-API-Key": DEVICE_API_KEY},
        )
        assert resp.status_code == 404

    @pytest.mark.asyncio
    async def test_missing_device_key_is_401(
        self, unauthed_client, seeded,
    ):
        resp = await unauthed_client.post(
            f"/api/devices/{DEVICE_ID}/logs/whatever/upload",
            content=b"x",
        )
        assert resp.status_code == 401

    @pytest.mark.asyncio
    async def test_wrong_device_key_is_403(
        self, app, unauthed_client, seeded,
    ):
        # Create another device with its own key and try to upload
        # against DEVICE_ID using that other key.
        other_key = "key-other-dev"
        factory = get_session_factory()
        async with factory() as db:
            d2 = Device(
                id="dev-other", name="Other",
                status=DeviceStatus.ADOPTED,
                device_api_key_hash=_hash_key(other_key),
            )
            db.add(d2)
            row = await log_outbox.create(db, device_id=DEVICE_ID)
            await db.commit()
            rid = row.id

        resp = await unauthed_client.post(
            f"/api/devices/{DEVICE_ID}/logs/{rid}/upload",
            content=b"x",
            headers={"X-Device-API-Key": other_key},
        )
        assert resp.status_code == 403
