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
from cms.models.log_request import STATUS_FAILED, STATUS_READY, STATUS_SENT
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


class TestLogsResponseFailureIsRecorded:
    """#889 — a crash while bundling the reply must not leave the row
    silently parked in ``sent`` with ``last_error`` still ``None``.

    The device already answered, so the drainer's stuck-``sent`` rescue
    (``claim_stuck_sent``, 15 min) is the only thing that ever moves the
    row again.  Recording the error keeps that retry intact while making
    the failure visible immediately, in the UI and in the smoke suite.
    """

    @pytest_asyncio.fixture
    async def _sent_row(self, app, _device):
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
            return row.id, settings

    async def _dispatch(self, settings, device_id, rid):
        factory = get_session_factory()
        async with factory() as db:
            device = await db.get(Device, device_id)
            ctx = InboundContext(
                device_id=device_id, device=device,
                device_name=device.name,
                base_url="http://test", settings=settings,
                group_id=None, group_name=None,
                device_status=device.status,
            )
            msg = {
                "type": MessageType.LOGS_RESPONSE.value,
                "request_id": rid,
                "device_id": device_id,
                "logs": {"agora-player": "player logs here"},
            }
            await dispatch_device_message(msg=msg, ctx=ctx, db=db, send=_noop_send)

    @pytest.mark.asyncio
    async def test_blob_write_failure_records_error_and_stays_retryable(
        self, app, _device, _sent_row, monkeypatch,
    ):
        rid, settings = _sent_row

        async def _boom(*args, **kwargs):
            raise RuntimeError("blob backend unavailable")

        monkeypatch.setattr("cms.services.log_blob.write_log_blob", _boom)
        await self._dispatch(settings, _device, rid)

        factory = get_session_factory()
        async with factory() as db:
            row = await log_outbox.get(db, rid)
            # Not terminal: the drainer's stuck-sent rescue still retries it.
            assert row.status == STATUS_SENT
            assert "blob backend unavailable" in (row.last_error or "")

    @pytest.mark.asyncio
    async def test_error_is_recorded_even_when_the_transaction_is_poisoned(
        self, app, _device, _sent_row, monkeypatch,
    ):
        """A DB-level failure leaves the session needing a rollback; the
        handler must roll back before writing ``last_error`` or the
        recording write is itself swallowed."""
        rid, settings = _sent_row

        async def _poison(db, *args, **kwargs):
            from sqlalchemy import text
            try:
                await db.execute(text("SELECT * FROM no_such_table_889"))
            except Exception:
                pass
            raise RuntimeError("mark_ready exploded")

        monkeypatch.setattr(log_outbox, "mark_ready", _poison)
        await self._dispatch(settings, _device, rid)

        factory = get_session_factory()
        async with factory() as db:
            row = await log_outbox.get(db, rid)
            assert row.status == STATUS_SENT
            assert "mark_ready exploded" in (row.last_error or "")
