"""Unit tests for :mod:`cms.services.log_outbox` (Stage 3a of #345).

Covers the outbox helper surface that backs the multi-replica-safe
``request_logs`` flow — row creation, status transitions, list helpers,
and the monotonic-ish guards that prevent lost updates when two
replicas race on the same row.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select

from cms.models.device import Device, DeviceStatus
from cms.models.log_request import (
    STATUS_EXPIRED,
    STATUS_FAILED,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_SENT,
    LogRequest,
)
from cms.services import log_outbox


async def _reload(db, request_id):
    """Re-fetch a LogRequest after a bulk UPDATE.

    Our helpers issue ``UPDATE ... WHERE ...`` statements which bypass
    the ORM identity map, so :func:`log_outbox.get` would otherwise
    return the stale instance cached in the session.  Production code
    avoids this by separating the commit from the subsequent read
    (usually across request boundaries); tests short-circuit with
    ``expire_all``.
    """
    db.expire_all()
    return await log_outbox.get(db, request_id)


@pytest_asyncio.fixture
async def _device(db_session):
    d = Device(id="d-outbox-1", name="Outbox Test", status=DeviceStatus.ADOPTED)
    db_session.add(d)
    await db_session.commit()
    return d


# ── create ──

class TestCreate:
    @pytest.mark.asyncio
    async def test_creates_pending_row_with_defaults(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()

        assert row.id  # UUID assigned
        assert row.device_id == _device.id
        assert row.status == STATUS_PENDING
        assert row.attempts == 0
        assert row.since == "24h"
        assert row.services is None
        assert row.sent_at is None
        assert row.ready_at is None
        assert row.blob_path is None
        assert row.size_bytes is None
        assert row.expires_at is not None  # default retention applied

    @pytest.mark.asyncio
    async def test_respects_provided_request_id(self, db_session, _device):
        row = await log_outbox.create(
            db_session, device_id=_device.id, request_id="fixed-rid-1",
        )
        await db_session.commit()
        assert row.id == "fixed-rid-1"

    @pytest.mark.asyncio
    async def test_respects_provided_services_and_since(self, db_session, _device):
        row = await log_outbox.create(
            db_session,
            device_id=_device.id,
            services=["agora-player", "agora-api"],
            since="7d",
        )
        await db_session.commit()
        assert row.services == ["agora-player", "agora-api"]
        assert row.since == "7d"

    @pytest.mark.asyncio
    async def test_expires_in_none_leaves_expires_at_null(self, db_session, _device):
        row = await log_outbox.create(
            db_session, device_id=_device.id, expires_in=None,
        )
        await db_session.commit()
        assert row.expires_at is None

    @pytest.mark.asyncio
    async def test_unknown_device_raises(self, db_session):
        # FK violation — conftest enables SQLite PRAGMA foreign_keys so
        # the check fires at flush (Postgres) or commit (SQLite).
        with pytest.raises(Exception):
            await log_outbox.create(db_session, device_id="does-not-exist")
            await db_session.commit()


# ── get ──

class TestGet:
    @pytest.mark.asyncio
    async def test_returns_row_when_present(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        fetched = await log_outbox.get(db_session, row.id)
        assert fetched is not None
        assert fetched.id == row.id

    @pytest.mark.asyncio
    async def test_returns_none_when_missing(self, db_session):
        fetched = await log_outbox.get(db_session, "no-such-id")
        assert fetched is None


# ── mark_sent ──

class TestMarkSent:
    @pytest.mark.asyncio
    async def test_transitions_pending_to_sent_and_bumps_attempts(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()

        ok = await log_outbox.mark_sent(db_session, row.id)
        await db_session.commit()

        assert ok is True
        refreshed = await _reload(db_session, row.id)
        assert refreshed.status == STATUS_SENT
        assert refreshed.attempts == 1
        assert refreshed.sent_at is not None

    @pytest.mark.asyncio
    async def test_second_call_is_noop_since_row_no_longer_pending(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()

        assert await log_outbox.mark_sent(db_session, row.id) is True
        await db_session.commit()
        # Second call — status is now 'sent', so the update guard matches zero rows.
        assert await log_outbox.mark_sent(db_session, row.id) is False
        await db_session.commit()

        refreshed = await _reload(db_session, row.id)
        assert refreshed.attempts == 1  # not bumped a second time

    @pytest.mark.asyncio
    async def test_missing_row_returns_false(self, db_session):
        assert await log_outbox.mark_sent(db_session, "no-such-id") is False


# ── mark_ready ──

class TestMarkReady:
    @pytest.mark.asyncio
    async def test_transitions_sent_to_ready_with_blob_metadata(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        await log_outbox.mark_sent(db_session, row.id)
        await db_session.commit()

        ok = await log_outbox.mark_ready(
            db_session, row.id,
            blob_path="device-logs/d-outbox-1/abc.tar.gz",
            size_bytes=12345,
        )
        await db_session.commit()

        assert ok is True
        refreshed = await _reload(db_session, row.id)
        assert refreshed.status == STATUS_READY
        assert refreshed.blob_path == "device-logs/d-outbox-1/abc.tar.gz"
        assert refreshed.size_bytes == 12345
        assert refreshed.ready_at is not None

    @pytest.mark.asyncio
    async def test_accepts_pending_to_ready_for_backcompat_shim(self, db_session, _device):
        # Stage 3b back-compat shim writes the legacy LOGS_RESPONSE
        # payload straight to blob without going through the drainer.
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        ok = await log_outbox.mark_ready(
            db_session, row.id, blob_path="x/y.tar.gz", size_bytes=1,
        )
        await db_session.commit()
        assert ok is True
        refreshed = await _reload(db_session, row.id)
        assert refreshed.status == STATUS_READY

    @pytest.mark.asyncio
    async def test_clears_last_error_on_ready(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        await log_outbox.record_attempt_error(db_session, row.id, error="boom")
        await db_session.commit()

        await log_outbox.mark_ready(
            db_session, row.id, blob_path="x/y.tar.gz", size_bytes=1,
        )
        await db_session.commit()

        refreshed = await _reload(db_session, row.id)
        assert refreshed.last_error is None

    @pytest.mark.asyncio
    async def test_does_not_transition_terminal_rows(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        await log_outbox.mark_failed(db_session, row.id, error="fatal")
        await db_session.commit()

        ok = await log_outbox.mark_ready(
            db_session, row.id, blob_path="x/y.tar.gz", size_bytes=1,
        )
        await db_session.commit()

        assert ok is False
        refreshed = await _reload(db_session, row.id)
        assert refreshed.status == STATUS_FAILED


# ── mark_failed ──

class TestMarkFailed:
    @pytest.mark.asyncio
    async def test_transitions_pending_to_failed(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()

        ok = await log_outbox.mark_failed(db_session, row.id, error="device offline")
        await db_session.commit()

        assert ok is True
        refreshed = await _reload(db_session, row.id)
        assert refreshed.status == STATUS_FAILED
        assert refreshed.last_error == "device offline"

    @pytest.mark.asyncio
    async def test_transitions_sent_to_failed(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        await log_outbox.mark_sent(db_session, row.id)
        await db_session.commit()

        ok = await log_outbox.mark_failed(db_session, row.id, error="upload timed out")
        await db_session.commit()

        assert ok is True
        refreshed = await _reload(db_session, row.id)
        assert refreshed.status == STATUS_FAILED

    @pytest.mark.asyncio
    async def test_does_not_overwrite_ready(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        await log_outbox.mark_ready(
            db_session, row.id, blob_path="x/y", size_bytes=1,
        )
        await db_session.commit()

        ok = await log_outbox.mark_failed(db_session, row.id, error="late")
        await db_session.commit()

        assert ok is False
        refreshed = await _reload(db_session, row.id)
        assert refreshed.status == STATUS_READY

    @pytest.mark.asyncio
    async def test_truncates_long_error_messages(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()

        long_err = "x" * 5000
        await log_outbox.mark_failed(db_session, row.id, error=long_err)
        await db_session.commit()

        refreshed = await _reload(db_session, row.id)
        assert len(refreshed.last_error) == 2000


# ── mark_expired ──

class TestMarkExpired:
    @pytest.mark.asyncio
    async def test_transitions_pending_to_expired(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()

        ok = await log_outbox.mark_expired(db_session, row.id)
        await db_session.commit()

        assert ok is True
        refreshed = await _reload(db_session, row.id)
        assert refreshed.status == STATUS_EXPIRED

    @pytest.mark.asyncio
    async def test_does_not_touch_terminal_rows(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        await log_outbox.mark_ready(
            db_session, row.id, blob_path="x/y", size_bytes=1,
        )
        await db_session.commit()

        ok = await log_outbox.mark_expired(db_session, row.id)
        await db_session.commit()

        assert ok is False
        refreshed = await _reload(db_session, row.id)
        assert refreshed.status == STATUS_READY


# ── record_attempt_error ──

class TestRecordAttemptError:
    @pytest.mark.asyncio
    async def test_bumps_attempts_and_stores_error_without_status_change(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()

        ok = await log_outbox.record_attempt_error(
            db_session, row.id, error="transport unavailable",
        )
        await db_session.commit()

        assert ok is True
        refreshed = await _reload(db_session, row.id)
        assert refreshed.status == STATUS_PENDING  # unchanged
        assert refreshed.attempts == 1
        assert refreshed.last_error == "transport unavailable"

    @pytest.mark.asyncio
    async def test_does_not_touch_non_pending_rows(self, db_session, _device):
        row = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        await log_outbox.mark_sent(db_session, row.id)
        await db_session.commit()

        ok = await log_outbox.record_attempt_error(
            db_session, row.id, error="should be ignored",
        )
        await db_session.commit()

        assert ok is False
        refreshed = await _reload(db_session, row.id)
        assert refreshed.last_error is None
        assert refreshed.attempts == 1  # only from mark_sent


# ── list_pending ──

class TestListPending:
    @pytest.mark.asyncio
    async def test_returns_pending_rows_in_created_order(self, db_session, _device):
        r1 = await log_outbox.create(db_session, device_id=_device.id)
        r2 = await log_outbox.create(db_session, device_id=_device.id)
        r3 = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()

        pending = await log_outbox.list_pending(db_session)
        ids = [r.id for r in pending]
        assert ids == [r1.id, r2.id, r3.id]

    @pytest.mark.asyncio
    async def test_excludes_non_pending_rows(self, db_session, _device):
        r1 = await log_outbox.create(db_session, device_id=_device.id)
        r2 = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        await log_outbox.mark_sent(db_session, r1.id)
        await db_session.commit()

        pending = await log_outbox.list_pending(db_session)
        assert [r.id for r in pending] == [r2.id]

    @pytest.mark.asyncio
    async def test_respects_limit(self, db_session, _device):
        for _ in range(5):
            await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()

        pending = await log_outbox.list_pending(db_session, limit=2)
        assert len(pending) == 2

    @pytest.mark.asyncio
    async def test_max_attempts_filters_exhausted_rows(self, db_session, _device):
        r1 = await log_outbox.create(db_session, device_id=_device.id)
        r2 = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        # Simulate r1 having hit the retry budget.
        await log_outbox.record_attempt_error(db_session, r1.id, error="e")
        await log_outbox.record_attempt_error(db_session, r1.id, error="e")
        await log_outbox.record_attempt_error(db_session, r1.id, error="e")
        await db_session.commit()

        pending = await log_outbox.list_pending(db_session, max_attempts=3)
        ids = [r.id for r in pending]
        assert r2.id in ids
        assert r1.id not in ids


# ── list_expired ──

class TestListExpired:
    @pytest.mark.asyncio
    async def test_returns_rows_past_expiry(self, db_session, _device):
        # Past expiry
        r1 = await log_outbox.create(
            db_session, device_id=_device.id, expires_in=timedelta(seconds=-1),
        )
        # Future expiry
        r2 = await log_outbox.create(
            db_session, device_id=_device.id, expires_in=timedelta(days=30),
        )
        # No expiry
        await log_outbox.create(
            db_session, device_id=_device.id, expires_in=None,
        )
        await db_session.commit()

        expired = await log_outbox.list_expired(db_session)
        ids = [r.id for r in expired]
        assert r1.id in ids
        assert r2.id not in ids

    @pytest.mark.asyncio
    async def test_excludes_null_expires_at(self, db_session, _device):
        await log_outbox.create(
            db_session, device_id=_device.id, expires_in=None,
        )
        await db_session.commit()

        expired = await log_outbox.list_expired(db_session)
        assert expired == []


# ── list_for_device ──

class TestListForDevice:
    @pytest.mark.asyncio
    async def test_filters_by_device_and_sorts_newest_first(self, db_session, _device):
        # Second device so we can assert filtering.
        other = Device(id="d-other", name="Other", status=DeviceStatus.ADOPTED)
        db_session.add(other)
        await db_session.commit()

        r1 = await log_outbox.create(db_session, device_id=_device.id)
        await log_outbox.create(db_session, device_id=other.id)
        r3 = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()

        rows = await log_outbox.list_for_device(db_session, _device.id)
        ids = [r.id for r in rows]
        assert ids == [r3.id, r1.id]


# ── count_by_status ──

class TestCountByStatus:
    @pytest.mark.asyncio
    async def test_returns_counts_per_status(self, db_session, _device):
        r1 = await log_outbox.create(db_session, device_id=_device.id)
        r2 = await log_outbox.create(db_session, device_id=_device.id)
        r3 = await log_outbox.create(db_session, device_id=_device.id)
        await db_session.commit()
        await log_outbox.mark_sent(db_session, r1.id)
        await log_outbox.mark_ready(db_session, r1.id, blob_path="x", size_bytes=1)
        await log_outbox.mark_failed(db_session, r2.id, error="e")
        await db_session.commit()

        counts = await log_outbox.count_by_status(db_session)
        assert counts.get(STATUS_READY) == 1
        assert counts.get(STATUS_FAILED) == 1
        assert counts.get(STATUS_PENDING) == 1
