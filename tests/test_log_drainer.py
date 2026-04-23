"""Tests for :mod:`cms.services.log_drainer` (Stage 3d of #345).

Covers :func:`drain_once` — the single-tick workhorse — and a light
smoke test of :func:`run_loop`'s shutdown path.  Keeps things unit-y
(no HTTP / router) by driving the outbox helpers directly against
``db_session`` fixtures.

Avoids the cross-loop asyncpg bug: tests talk to the same loop as
pytest-asyncio, use ``db_session`` or a session from the test's own
factory, and never mix :class:`starlette.testclient.TestClient`
with async DB fixtures.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.models.device import Device, DeviceStatus
from cms.models.log_request import (
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,  # noqa: F401 — intentionally imported for parity with sibling tests
    STATUS_SENT,
    LogRequest,
)
from cms.services import log_drainer, log_outbox


# ── Fixtures ─────────────────────────────────────────────────────────


@pytest_asyncio.fixture
async def _device(db_session):
    """Seed a single adopted device for the outbox rows to reference."""
    d = Device(id="d-drainer-1", name="Drainer Test", status=DeviceStatus.ADOPTED)
    db_session.add(d)
    await db_session.commit()
    return d


def _make_settings(**overrides) -> SimpleNamespace:
    """Build a Settings-shaped object with the drainer knobs."""
    base = {
        "log_drainer_interval_sec": 5.0,
        "log_drainer_batch_size": 25,
        "log_drainer_sent_timeout_sec": 900,
        "log_drainer_max_attempts": 10,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class FakeTransport:
    """Minimal dispatcher — only ``dispatch_request_logs`` is exercised
    by the drainer.  Other ``DeviceTransport`` methods raise so the
    tests would fail loudly if the drainer started using them.
    """

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self.should_fail = False
        self.fail_exc: BaseException = ValueError("device offline")
        # Per-request-id override: failures keyed by request id win
        # over the global flag.  Lets test_drainer_continues_on_exception
        # fail exactly one row in a batch.
        self.fail_for_request_id: dict[str, BaseException] = {}

    async def dispatch_request_logs(
        self,
        device_id: str,
        *,
        request_id: str,
        services=None,
        since: str = "24h",
    ) -> None:
        self.calls.append((device_id, request_id, services, since))
        if request_id in self.fail_for_request_id:
            raise self.fail_for_request_id[request_id]
        if self.should_fail:
            raise self.fail_exc

    async def send_to_device(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    async def is_connected(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError

    async def connected_count(self):  # pragma: no cover
        raise NotImplementedError

    async def connected_ids(self):  # pragma: no cover
        raise NotImplementedError

    async def get_all_states(self):  # pragma: no cover
        raise NotImplementedError

    async def set_state_flags(self, *a, **kw):  # pragma: no cover
        raise NotImplementedError


async def _reload(db, request_id: str) -> LogRequest | None:
    """Re-fetch a row after bulk UPDATEs (our helpers bypass the
    identity map, so ORM-cached instances go stale).

    Uses ``populate_existing=True`` which forces the SELECT to
    overwrite the identity-mapped instance's attributes.  We avoid
    ``db.expire_all()`` because it would expire *all* other cached
    instances — subsequent attribute access on those would trigger
    sync refresh and trip MissingGreenlet on AsyncSession.
    """
    result = await db.execute(
        select(LogRequest)
        .where(LogRequest.id == request_id)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _set_row(db, request_id: str, **values) -> None:
    """Raw UPDATE helper for priming rows into specific states.

    The drainer's SELECTs use ``populate_existing=True`` so tests
    don't need to expire the session here — keeping the identity
    map intact means ``row.id`` stays accessible after this call.
    """
    await db.execute(
        update(LogRequest).where(LogRequest.id == request_id).values(**values)
    )
    await db.commit()


# ── Individual tick scenarios ────────────────────────────────────────


@pytest.mark.asyncio
async def test_drainer_dispatches_pending_row(db_session, _device):
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()

    transport = FakeTransport()
    stats = await log_drainer.drain_once(db_session, transport=transport)

    assert stats["claimed_pending"] == 1
    assert stats["dispatched"] == 1
    assert stats["failed"] == 0
    assert len(transport.calls) == 1

    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_SENT
    assert refreshed.attempts == 1
    assert refreshed.sent_at is not None


@pytest.mark.asyncio
async def test_drainer_records_transient_failure_and_keeps_pending(
    db_session, _device,
):
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()

    transport = FakeTransport()
    transport.should_fail = True
    transport.fail_exc = ValueError("device offline")

    stats = await log_drainer.drain_once(db_session, transport=transport)

    assert stats["claimed_pending"] == 1
    assert stats["dispatched"] == 0
    assert stats["failed"] == 0

    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_PENDING
    assert refreshed.attempts == 1
    assert refreshed.last_error == "device offline"


@pytest.mark.asyncio
async def test_drainer_marks_failed_after_max_attempts(db_session, _device):
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()
    settings = _make_settings(log_drainer_max_attempts=5)
    # Prime attempts = max - 1 so the next failure trips the budget.
    # Also backdate updated_at so the row is past its backoff window.
    await _set_row(
        db_session, row.id,
        attempts=settings.log_drainer_max_attempts - 1,
        updated_at=datetime.now(timezone.utc) - timedelta(hours=2),
    )

    transport = FakeTransport()
    transport.should_fail = True

    stats = await log_drainer.drain_once(
        db_session, transport=transport, settings=settings,
    )

    assert stats["failed"] == 1
    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_FAILED
    assert refreshed.last_error


@pytest.mark.asyncio
async def test_drainer_respects_backoff(db_session, _device):
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()

    # attempts=3 → backoff window = 60 * 2**3 = 480s.  updated_at is
    # 30s in the past → 450s still to wait before retry.
    now = datetime.now(timezone.utc)
    await _set_row(
        db_session, row.id,
        attempts=3,
        updated_at=now - timedelta(seconds=30),
    )

    transport = FakeTransport()

    # Inside the backoff window → no dispatch.
    stats = await log_drainer.drain_once(
        db_session, transport=transport, now=now,
    )
    assert stats["claimed_pending"] == 0
    assert stats["dispatched"] == 0
    assert transport.calls == []

    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_PENDING
    assert refreshed.attempts == 3  # unchanged

    # Advance past the backoff window → row is picked up.
    future = now + timedelta(seconds=500)
    stats = await log_drainer.drain_once(
        db_session, transport=transport, now=future,
    )
    assert stats["dispatched"] == 1

    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_SENT
    assert refreshed.attempts == 4  # 3 + 1 from mark_sent


@pytest.mark.asyncio
async def test_drainer_rescues_stuck_sent(db_session, _device):
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()

    # Put it into the sent state and push sent_at 20 min into the past.
    now = datetime.now(timezone.utc)
    await _set_row(
        db_session, row.id,
        status=STATUS_SENT,
        attempts=1,
        sent_at=now - timedelta(minutes=20),
        updated_at=now - timedelta(minutes=20),
    )

    transport = FakeTransport()
    stats = await log_drainer.drain_once(
        db_session, transport=transport, now=now,
    )
    assert stats["rescued_sent"] == 1
    # Rescued rows are not re-dispatched in the same tick.
    assert stats["dispatched"] == 0

    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_PENDING

    # Next tick picks up the now-pending row.  attempts was 1 so the
    # backoff window is 120s — advance ``now`` well past it.
    stats = await log_drainer.drain_once(
        db_session,
        transport=transport,
        now=now + timedelta(seconds=600),
    )
    assert stats["dispatched"] == 1
    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_SENT


@pytest.mark.asyncio
async def test_drainer_does_not_rescue_fresh_sent(db_session, _device):
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()

    now = datetime.now(timezone.utc)
    await _set_row(
        db_session, row.id,
        status=STATUS_SENT,
        attempts=1,
        sent_at=now - timedelta(minutes=5),
        updated_at=now - timedelta(minutes=5),
    )

    transport = FakeTransport()
    stats = await log_drainer.drain_once(
        db_session, transport=transport, now=now,
    )
    assert stats["rescued_sent"] == 0

    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_SENT


@pytest.mark.asyncio
async def test_drainer_fails_stuck_sent_over_max_attempts(db_session, _device):
    settings = _make_settings(log_drainer_max_attempts=5)
    row = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()

    now = datetime.now(timezone.utc)
    await _set_row(
        db_session, row.id,
        status=STATUS_SENT,
        attempts=settings.log_drainer_max_attempts,
        sent_at=now - timedelta(minutes=30),
        updated_at=now - timedelta(minutes=30),
    )

    transport = FakeTransport()
    stats = await log_drainer.drain_once(
        db_session, transport=transport, settings=settings, now=now,
    )
    assert stats["failed"] == 1
    assert stats["rescued_sent"] == 0

    refreshed = await _reload(db_session, row.id)
    assert refreshed.status == STATUS_FAILED


@pytest.mark.asyncio
async def test_drainer_batch_size_caps_work(db_session, _device):
    ids: list[str] = []
    for _ in range(30):
        row = await log_outbox.create(db_session, device_id=_device.id)
        ids.append(row.id)
    await db_session.commit()

    settings = _make_settings(log_drainer_batch_size=10)
    transport = FakeTransport()
    stats = await log_drainer.drain_once(
        db_session, transport=transport, settings=settings,
    )
    assert stats["claimed_pending"] == 10
    assert stats["dispatched"] == 10

    # 20 rows remain pending.
    db_session.expire_all()
    rows = (
        await db_session.execute(
            select(LogRequest).where(LogRequest.status == STATUS_PENDING)
        )
    ).scalars().all()
    assert len(rows) == 20


@pytest.mark.asyncio
async def test_drainer_continues_on_exception(db_session, _device):
    r1 = await log_outbox.create(db_session, device_id=_device.id)
    r2 = await log_outbox.create(db_session, device_id=_device.id)
    r3 = await log_outbox.create(db_session, device_id=_device.id)
    await db_session.commit()
    # Capture ids upfront — later expire_all() calls in _reload leave
    # the ORM instances with expired attrs, and accessing row.id would
    # trigger a sync refresh (MissingGreenlet on AsyncSession).
    r1_id, r2_id, r3_id = r1.id, r2.id, r3.id

    transport = FakeTransport()
    # Fail exactly r2 with a non-ValueError — gather(return_exceptions=True)
    # must let r1 and r3 succeed.
    transport.fail_for_request_id[r2_id] = RuntimeError("boom")

    stats = await log_drainer.drain_once(db_session, transport=transport)
    assert stats["claimed_pending"] == 3
    assert stats["dispatched"] == 2
    assert stats["failed"] == 0  # under max_attempts → record_attempt_error

    refreshed_r1 = await _reload(db_session, r1_id)
    refreshed_r2 = await _reload(db_session, r2_id)
    refreshed_r3 = await _reload(db_session, r3_id)
    assert refreshed_r1.status == STATUS_SENT
    assert refreshed_r2.status == STATUS_PENDING
    assert refreshed_r2.attempts == 1
    assert refreshed_r3.status == STATUS_SENT


# ── run_loop lifecycle ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_drainer_loop_stops_on_event(db_engine):
    """``run_loop`` must exit promptly when ``stop_event`` is set."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    transport = FakeTransport()
    settings = _make_settings(log_drainer_interval_sec=0.05)
    stop_event = asyncio.Event()

    task = asyncio.create_task(
        log_drainer.run_loop(
            lambda: factory,
            lambda: transport,
            settings=settings,
            stop_event=stop_event,
        )
    )
    # Let the loop complete at least one tick.
    await asyncio.sleep(0.1)
    stop_event.set()
    await asyncio.wait_for(task, timeout=2.0)
    assert task.done()
