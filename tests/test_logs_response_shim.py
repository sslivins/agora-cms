"""Stage 3b back-compat shim tests (#345).

When legacy Pi firmware sends a LOGS_RESPONSE frame over WS, the
inbound handler still resolves the in-flight future (preserving
/api/logs/download) *and* — when the request_id matches an outbox
row in pending/sent — writes a tar.gz bundle to blob storage and
flips the row to ready.  This test drives the handler directly and
asserts both outcomes.
"""

from __future__ import annotations

import io
import tarfile
from pathlib import Path

import pytest
import pytest_asyncio

from cms.database import get_session_factory
from cms.models.device import Device, DeviceStatus
from cms.models.log_request import STATUS_FAILED, STATUS_READY
from cms.schemas.protocol import MessageType
from cms.services import log_outbox
from cms.services.device_inbound import InboundContext, dispatch_device_message


async def _noop_send(msg: dict) -> None:
    return None


@pytest_asyncio.fixture
async def _device(app):
    factory = get_session_factory()
    async with factory() as db:
        dev = Device(
            id="dev-shim-1", name="Shim", status=DeviceStatus.ADOPTED,
        )
        db.add(dev)
        await db.commit()
    return "dev-shim-1"


@pytest.mark.asyncio
async def test_logs_response_shim_writes_blob_and_marks_ready(app, _device):
    from cms.services.log_blob import (
        LocalLogBlobBackend, init_log_storage, set_log_backend,
    )
    from cms.auth import get_settings

    settings = app.dependency_overrides[get_settings]()
    set_log_backend(LocalLogBlobBackend(base_path=settings.asset_storage_path))
    await init_log_storage(settings)

    factory = get_session_factory()
    async with factory() as db:
        row = await log_outbox.create(db, device_id=_device)
        await log_outbox.mark_sent(db, row.id)
        await db.commit()
        rid = row.id

    # Drive the handler with a synthetic LOGS_RESPONSE.
    async with factory() as db:
        device = await db.get(Device, _device)
        ctx = InboundContext(
            device_id=_device, device=device,
            device_name=device.name,
            base_url="http://test", settings=settings,
            group_id=None, group_name=None,
            device_status=device.status,
        )
        msg = {
            "type": MessageType.LOGS_RESPONSE.value,
            "request_id": rid,
            "device_id": _device,
            "logs": {
                "agora-player": "player logs here",
                "agora-api": "api logs here",
            },
        }
        await dispatch_device_message(msg=msg, ctx=ctx, db=db, send=_noop_send)

    # Outbox row should be ready and blob should exist.
    async with factory() as db:
        row = await log_outbox.get(db, rid)
        assert row.status == STATUS_READY
        assert row.blob_path == f"{_device}/{rid}.tar.gz"
        assert row.size_bytes and row.size_bytes > 0

    blob_file = (
        Path(settings.asset_storage_path)
        / "device-logs" / _device / f"{rid}.tar.gz"
    )
    assert blob_file.is_file()
    # Verify the bundle contains our services.
    with tarfile.open(fileobj=io.BytesIO(blob_file.read_bytes()), mode="r:gz") as tf:
        names = set(tf.getnames())
        assert "agora-player.log" in names
        assert "agora-api.log" in names


@pytest.mark.asyncio
async def test_logs_response_shim_with_error_marks_failed(app, _device):
    from cms.services.log_blob import (
        LocalLogBlobBackend, init_log_storage, set_log_backend,
    )
    from cms.auth import get_settings

    settings = app.dependency_overrides[get_settings]()
    set_log_backend(LocalLogBlobBackend(base_path=settings.asset_storage_path))
    await init_log_storage(settings)

    factory = get_session_factory()
    async with factory() as db:
        row = await log_outbox.create(db, device_id=_device)
        await log_outbox.mark_sent(db, row.id)
        await db.commit()
        rid = row.id

    async with factory() as db:
        device = await db.get(Device, _device)
        ctx = InboundContext(
            device_id=_device, device=device,
            device_name=device.name,
            base_url="http://test", settings=settings,
            group_id=None, group_name=None,
            device_status=device.status,
        )
        msg = {
            "type": MessageType.LOGS_RESPONSE.value,
            "request_id": rid,
            "device_id": _device,
            "logs": {},
            "error": "journalctl not installed",
        }
        await dispatch_device_message(msg=msg, ctx=ctx, db=db, send=_noop_send)

    async with factory() as db:
        row = await log_outbox.get(db, rid)
        assert row.status == STATUS_FAILED
        assert "journalctl" in (row.last_error or "")
