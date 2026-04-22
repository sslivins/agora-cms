"""Tests for :mod:`cms.services.log_reaper` (Stage 3e of #345).

Covers :func:`reap_once` — the single-tick workhorse — and a light
smoke test of :func:`run_loop`'s shutdown path.  Structured to mirror
``test_log_drainer.py``: drives the outbox helpers against
``db_session`` fixtures and injects a fake blob deleter.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, update

from cms.models.device import Device, DeviceStatus
from cms.models.log_request import (
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_SENT,
    LogRequest,
)
from cms.services import log_outbox, log_reaper


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def _device(db_session):
    """Seed a single adopted device for outbox rows to reference."""
    d = Device(id="d-reaper-1", name="Reaper Test", status=DeviceStatus.ADOPTED)
    db_session.add(d)
    await db_session.commit()
    return d


def _make_settings(**overrides) -> SimpleNamespace:
    base = {
        "log_reaper_interval_sec": 600.0,
        "log_reaper_batch_size": 100,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeBlobDeleter:
    """Records each ``delete`` call.  Return value is configurable
    per-path via ``missing`` (backend says the blob wasn't there),
    or globally via ``should_fail`` to raise.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.missing: set[str] = set()
        self.should_fail = False
        self.fail_exc: BaseException = RuntimeError("blob backend down")

    async def __call__(self, relative_path: str) -> bool:
        self.calls.append(relative_path)
        if self.should_fail:
            raise self.fail_exc
        return relative_path not in self.missing


async def _reload(db, request_id: str) -> LogRequest | None:
    result = await db.execute(
        select(LogRequest)
        .where(LogRequest.id == request_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _set_row(db, request_id: str, **values) -> None:
    await db.execute(
        update(LogRequest).where(LogRequest.id == request_id).values(**values)
    )
    await db.commit()


def _past(seconds: int = 60) -> datetime:
    return datetime.now(timezone.utc) - timedelta(seconds=seconds)


def _future(seconds: int = 60) -> datetime:
    return datetime.now(timezone.utc) + timedelta(seconds=seconds)


# ── Individual tick scenarios ────────────────────────────────────────


@pytest.mark.asyncio
async def test_reaps_expired_row_with_blob(db_session, _device):
    """Happy path: an expired ``ready`` row with a blob gets its blob
    deleted, flips to ``expired``, and ``blob_path`` is cleared."""
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()
    blob_path = f"device-logs/{_device.id}/{row.id}.tar.gz"
    await _set_row(
        db_session,
        row.id,
        status=STATUS_READY,
        blob_path=blob_path,
        expires_at=_past(),
    )

    deleter = FakeBlobDeleter()
    stats = await log_reaper.reap_once(db_session, blob_deleter=deleter)

    assert stats["claimed"] == 1
    assert stats["blobs_deleted"] == 1
    assert stats["blobs_missing"] == 0
    assert stats["rows_expired"] == 1
    assert stats["errors"] == 0
    assert deleter.calls == [blob_path]

    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_EXPIRED
    assert refreshed.blob_path is None


@pytest.mark.asyncio
async def test_reaps_expired_row_without_blob(db_session, _device):
    """A failed row with NULL ``blob_path`` still transitions to
    ``expired`` — no blob call is issued."""
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()
    await _set_row(
        db_session,
        row.id,
        status=STATUS_FAILED,
        blob_path=None,
        expires_at=_past(),
    )

    deleter = FakeBlobDeleter()
    stats = await log_reaper.reap_once(db_session, blob_deleter=deleter)

    assert stats["claimed"] == 1
    assert stats["blobs_deleted"] == 0
    assert stats["rows_expired"] == 1
    assert deleter.calls == []

    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_EXPIRED


@pytest.mark.asyncio
async def test_skips_rows_not_yet_expired(db_session, _device):
    """``expires_at`` in the future → row untouched, no stats."""
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()
    await _set_row(
        db_session,
        row.id,
        status=STATUS_READY,
        blob_path="device-logs/x/y.tar.gz",
        expires_at=_future(3600),
    )

    deleter = FakeBlobDeleter()
    stats = await log_reaper.reap_once(db_session, blob_deleter=deleter)

    assert stats["claimed"] == 0
    assert stats["rows_expired"] == 0
    assert deleter.calls == []

    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_READY
    assert refreshed.blob_path == "device-logs/x/y.tar.gz"


@pytest.mark.asyncio
async def test_skips_rows_already_expired(db_session, _device):
    """Idempotence — a row already in ``expired`` is filtered out by
    the claim query so the deleter isn't called twice."""
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()
    await _set_row(
        db_session,
        row.id,
        status=STATUS_EXPIRED,
        blob_path=None,
        expires_at=_past(),
    )

    deleter = FakeBlobDeleter()
    stats = await log_reaper.reap_once(db_session, blob_deleter=deleter)

    assert stats["claimed"] == 0
    assert deleter.calls == []


@pytest.mark.asyncio
async def test_skips_rows_with_null_expires_at(db_session, _device):
    """``expires_at IS NULL`` (opt-out / forensic retention) is always
    excluded from the reaper scan."""
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()
    await _set_row(
        db_session,
        row.id,
        status=STATUS_READY,
        blob_path="device-logs/forever/keep.tar.gz",
        expires_at=None,
    )

    deleter = FakeBlobDeleter()
    stats = await log_reaper.reap_once(db_session, blob_deleter=deleter)

    assert stats["claimed"] == 0
    assert deleter.calls == []


@pytest.mark.asyncio
async def test_missing_blob_is_benign(db_session, _device):
    """Backend returns ``False`` (blob already gone) → still counts as
    a successful expiry; ``blobs_missing`` is bumped for visibility."""
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()
    blob_path = f"device-logs/{_device.id}/{row.id}.tar.gz"
    await _set_row(
        db_session,
        row.id,
        status=STATUS_READY,
        blob_path=blob_path,
        expires_at=_past(),
    )

    deleter = FakeBlobDeleter()
    deleter.missing.add(blob_path)
    stats = await log_reaper.reap_once(db_session, blob_deleter=deleter)

    assert stats["claimed"] == 1
    assert stats["blobs_deleted"] == 0
    assert stats["blobs_missing"] == 1
    assert stats["rows_expired"] == 1

    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_EXPIRED
    assert refreshed.blob_path is None


@pytest.mark.asyncio
async def test_blob_delete_error_keeps_row(db_session, _device):
    """Transient backend error → row is NOT marked expired.  The
    next tick retries from the same state."""
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()
    blob_path = f"device-logs/{_device.id}/{row.id}.tar.gz"
    await _set_row(
        db_session,
        row.id,
        status=STATUS_READY,
        blob_path=blob_path,
        expires_at=_past(),
    )

    deleter = FakeBlobDeleter()
    deleter.should_fail = True
    stats = await log_reaper.reap_once(db_session, blob_deleter=deleter)

    assert stats["claimed"] == 1
    assert stats["blobs_deleted"] == 0
    assert stats["rows_expired"] == 0
    assert stats["errors"] == 1

    refreshed = await _reload(db_session, row.id)
    # Row untouched — blob_path + status preserved.
    assert refreshed.status == STATUS_READY
    assert refreshed.blob_path == blob_path


@pytest.mark.asyncio
async def test_batch_size_caps_work_per_tick(db_session, _device):
    """``log_reaper_batch_size`` limits how many rows one tick
    touches.  Remaining rows ride the next tick."""
    ids: list[str] = []
    for _ in range(5):
        r = await log_outbox.create(db_session, device_id=_device.id)
        ids.append(r.id)
    await db_session.commit()
    for rid in ids:
        await _set_row(
            db_session,
            rid,
            status=STATUS_READY,
            blob_path=f"device-logs/{_device.id}/{rid}.tar.gz",
            expires_at=_past(),
        )

    deleter = FakeBlobDeleter()
    settings = _make_settings(log_reaper_batch_size=2)
    stats = await log_reaper.reap_once(
        db_session, blob_deleter=deleter, settings=settings,
    )

    assert stats["claimed"] == 2
    assert stats["rows_expired"] == 2
    assert len(deleter.calls) == 2

    # Second tick mops up two more.
    stats2 = await log_reaper.reap_once(
        db_session, blob_deleter=deleter, settings=settings,
    )
    assert stats2["claimed"] == 2

    # Third tick: one row left.
    stats3 = await log_reaper.reap_once(
        db_session, blob_deleter=deleter, settings=settings,
    )
    assert stats3["claimed"] == 1

    # Fourth tick: nothing.
    stats4 = await log_reaper.reap_once(
        db_session, blob_deleter=deleter, settings=settings,
    )
    assert stats4["claimed"] == 0


@pytest.mark.asyncio
async def test_mixed_states_all_transition_to_expired(db_session, _device):
    """Rows in ``pending``, ``sent``, ``ready``, and ``failed`` all
    flip to ``expired`` once past ``expires_at``.  Matches the spec:
    ``expires_at`` is the authoritative retention signal regardless
    of the row's prior terminal-ness."""
    states = [STATUS_PENDING, STATUS_SENT, STATUS_READY, STATUS_FAILED]
    ids: list[str] = []
    for state in states:
        r = await log_outbox.create(db_session, device_id=_device.id)
        ids.append(r.id)
        await db_session.commit()
        await _set_row(
            db_session,
            r.id,
            status=state,
            blob_path=f"device-logs/{_device.id}/{r.id}.tar.gz" if state == STATUS_READY else None,
            expires_at=_past(),
        )

    deleter = FakeBlobDeleter()
    stats = await log_reaper.reap_once(db_session, blob_deleter=deleter)

    assert stats["claimed"] == len(states)
    assert stats["rows_expired"] == len(states)
    assert stats["blobs_deleted"] == 1  # only the READY row had a blob

    for rid in ids:
        refreshed = await _reload(db_session, rid)
        assert refreshed.status == STATUS_EXPIRED
        assert refreshed.blob_path is None


# ── Loop smoke test ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_loop_stops_on_event(db_session):
    """``run_loop`` exits cleanly when ``stop_event`` is set before the
    first interval elapses."""
    stop = asyncio.Event()
    settings = _make_settings(log_reaper_interval_sec=0.05)

    # Factory returns None → loop skips the tick and hits the sleep.
    task = asyncio.create_task(
        log_reaper.run_loop(
            lambda: None,
            settings=settings,
            stop_event=stop,
        )
    )
    # Let one or two ticks run, then ask it to stop.
    await asyncio.sleep(0.15)
    stop.set()
    await asyncio.wait_for(task, timeout=1.0)
    assert task.done()
