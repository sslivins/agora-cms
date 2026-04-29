"""Behavioural tests for scheduler-side telemetry counters (Pillar B2).

These tests verify that ``cms.services.scheduler`` emits the expected
:class:`opentelemetry.metrics.Counter` ``.add()`` calls under each
operational outcome.  The strategy is to **monkeypatch the counter's
``add`` method** so we capture the exact call arguments without
needing a configured OTel SDK and without coupling to the registry's
internal types.

What we cover (per the Pillar B2 plan):

* ``agora.scheduler.tick`` — exactly one ``.add(1, {outcome=…})`` per
  scheduler-loop iteration:
  - leader, ``evaluate_schedules`` returns successfully → ``evaluated``,
  - non-leader replica → ``skipped_not_leader``,
  - leader, ``evaluate_schedules`` raises :class:`Exception` → ``error``,
  - leader, ``evaluate_schedules`` raises :class:`asyncio.CancelledError`
    → **no** ``.add()`` call (cancellation is not a tick outcome).

* ``agora.scheduler.missed_emitted`` — incremented exactly once per
  tick by the count of MISSED events whose ``ScheduleLog`` row was
  durably committed.  Key edge cases:
  - happy path → one ``.add(N)`` call after the commit,
  - log-write failure → CAS claim is reverted, so **no** ``.add()``
    call (the count stays at zero), and
  - zero MISSED events → no ``.add(0)`` (avoids polluting App Insights
    with zero-valued series).
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta, timezone

import pytest

from cms.models.asset import Asset, AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.models.schedule_log import ScheduleLog
from cms.models.schedule_missed_event import ScheduleMissedEvent
from cms.models.setting import CMSSetting
from cms.services.scheduler import (
    MISSED_GRACE_SECONDS,
    evaluate_schedules,
    scheduler_loop,
)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _FakeLease:
    """Drop-in replacement for ``cms.services.leader.LeaderLease``.

    Lets the test choose whether the replica is the leader without
    bringing up the real lease (which requires Postgres advisory
    locks).  ``start``/``stop`` are no-ops so the loop's
    ``try/finally`` cleanup is happy.
    """

    def __init__(self, *_a, is_leader: bool = True, **_kw):
        self._is_leader = is_leader

    @property
    def is_leader(self) -> bool:
        return self._is_leader

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


async def _run_one_tick(
    monkeypatch: pytest.MonkeyPatch,
    *,
    is_leader: bool,
    evaluate_side_effect=None,
):
    """Run exactly one iteration of ``scheduler_loop``.

    The loop is an infinite ``while True``; we cancel it during the
    post-tick ``asyncio.sleep`` so the *first* tick has fully run
    (counter incremented in the ``finally`` block) but the loop
    doesn't run a second time.
    """
    import cms.services.scheduler as scheduler_mod

    # Patch the LeaderLease class used inside the function (it's
    # imported lazily at the top of ``scheduler_loop``).
    monkeypatch.setattr(
        "cms.services.leader.LeaderLease",
        lambda *a, **kw: _FakeLease(is_leader=is_leader),
    )

    # Replace ``evaluate_schedules`` with a stub so we don't need a
    # real DB session.  These tests are about *the loop's own*
    # outcome attribution, not about evaluate_schedules' behaviour.
    async def _stub(*_a, **_kw):
        if evaluate_side_effect is not None:
            raise evaluate_side_effect
        return None

    monkeypatch.setattr(scheduler_mod, "evaluate_schedules", _stub)

    # Cancel inside the post-tick sleep so the ``finally`` block has
    # already incremented the tick counter (or, for the cancellation
    # test, so the body's own raise propagates first).
    async def _short_sleep(*_a, **_kw):
        raise asyncio.CancelledError()

    monkeypatch.setattr(scheduler_mod.asyncio, "sleep", _short_sleep)

    task = asyncio.create_task(scheduler_loop())
    try:
        await asyncio.wait_for(task, timeout=2.0)
    except asyncio.CancelledError:
        pass
    except asyncio.TimeoutError:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


# ----------------------------------------------------------------------
# Tick counter (scheduler_loop)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tick_counter_records_evaluated_when_leader_succeeds(monkeypatch):
    from cms import metrics

    calls: list[tuple] = []
    monkeypatch.setattr(
        metrics.scheduler_tick_total,
        "add",
        lambda *a, **kw: calls.append((a, kw)),
    )

    await _run_one_tick(monkeypatch, is_leader=True)

    assert len(calls) == 1, calls
    args, _kw = calls[0]
    amount, attrs = args
    assert amount == 1
    assert attrs == {metrics.ATTR_OUTCOME: metrics.SCHEDULER_OUTCOME_EVALUATED}


@pytest.mark.asyncio
async def test_tick_counter_records_skipped_when_not_leader(monkeypatch):
    from cms import metrics

    calls: list[tuple] = []
    monkeypatch.setattr(
        metrics.scheduler_tick_total,
        "add",
        lambda *a, **kw: calls.append((a, kw)),
    )

    await _run_one_tick(monkeypatch, is_leader=False)

    assert len(calls) == 1
    amount, attrs = calls[0][0]
    assert amount == 1
    assert attrs == {
        metrics.ATTR_OUTCOME: metrics.SCHEDULER_OUTCOME_SKIPPED_NOT_LEADER,
    }


@pytest.mark.asyncio
async def test_tick_counter_records_error_on_evaluate_exception(monkeypatch):
    from cms import metrics

    calls: list[tuple] = []
    monkeypatch.setattr(
        metrics.scheduler_tick_total,
        "add",
        lambda *a, **kw: calls.append((a, kw)),
    )

    await _run_one_tick(
        monkeypatch,
        is_leader=True,
        evaluate_side_effect=RuntimeError("simulated scheduler boom"),
    )

    assert len(calls) == 1
    amount, attrs = calls[0][0]
    assert amount == 1
    assert attrs == {metrics.ATTR_OUTCOME: metrics.SCHEDULER_OUTCOME_ERROR}


@pytest.mark.asyncio
async def test_tick_counter_does_not_record_on_cancelled_error(monkeypatch):
    """``CancelledError`` must propagate without emitting a tick metric.

    Cancellation is the normal shutdown path (lifespan teardown calls
    ``task.cancel()``).  Recording it as a tick outcome would make the
    error-rate workbook noisy at every restart.
    """
    from cms import metrics

    calls: list[tuple] = []
    monkeypatch.setattr(
        metrics.scheduler_tick_total,
        "add",
        lambda *a, **kw: calls.append((a, kw)),
    )

    await _run_one_tick(
        monkeypatch,
        is_leader=True,
        evaluate_side_effect=asyncio.CancelledError(),
    )

    assert calls == [], (
        "CancelledError must NOT increment the tick counter — it is "
        "the normal shutdown path, not a real outcome"
    )


# ----------------------------------------------------------------------
# Missed-emitted counter (evaluate_schedules)
# ----------------------------------------------------------------------


@pytest.mark.asyncio
class TestMissedEmittedCounter:
    """Behavioural tests for ``agora.scheduler.missed_emitted``.

    These run against the real ``evaluate_schedules`` and the real DB
    fixture (same pattern as ``TestScheduleMissedEvents`` in
    ``test_scheduler_advanced.py``) because the increment is
    inseparable from the CAS-claim + commit boundary; a unit-style
    stub would not exercise that boundary.
    """

    async def test_missed_emitted_increments_after_commit(
        self, app, db_session, monkeypatch,
    ):
        from cms.services.device_manager import device_manager
        from cms import metrics

        calls: list[tuple] = []
        monkeypatch.setattr(
            metrics.scheduler_missed_emitted_total,
            "add",
            lambda *a, **kw: calls.append((a, kw)),
        )

        setting = CMSSetting(key="timezone", value="UTC")
        asset = Asset(
            filename="b2_metric.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="b2metric1",
        )
        group = DeviceGroup(name="B2 Metric Group")
        device = Device(
            id="b2-metric-dev-01", name="B2 Metric Device",
            status=DeviceStatus.ADOPTED,
        )
        dummy = Device(
            id="b2-metric-dummy", name="B2 Metric Dummy",
            status=DeviceStatus.ADOPTED,
        )
        db_session.add_all([setting, asset, group, device, dummy])
        await db_session.flush()
        device.group_id = group.id

        sched = Schedule(
            name="B2 Metric Test",
            group_id=group.id,
            asset_id=asset.id,
            start_time=time(0, 0),
            end_time=time(23, 59, 59),
            priority=10,
            enabled=True,
        )
        db_session.add(sched)
        await db_session.commit()

        class FakeWS:
            async def send_json(self, data):
                pass

        device_manager.register("b2-metric-dummy", FakeWS())

        try:
            now = datetime.now(timezone.utc)
            db_session.add(ScheduleMissedEvent(
                schedule_id=sched.id,
                device_id="b2-metric-dev-01",
                occurrence_date=now.date(),
                first_seen_offline_at=now - timedelta(
                    seconds=MISSED_GRACE_SECONDS + 10,
                ),
                emitted_at=None,
            ))
            await db_session.commit()

            await evaluate_schedules()
        finally:
            device_manager.disconnect("b2-metric-dummy")

        assert len(calls) == 1, (
            f"expected exactly one .add() call after the commit, got {calls!r}"
        )
        args, _kw = calls[0]
        # The counter is a positional-only ``.add(amount, [attrs])``;
        # for missed_emitted we deliberately pass no attributes.
        assert args[0] == 1, (
            f"expected one MISSED row to have been written, got count={args[0]}"
        )

    async def test_missed_emitted_not_recorded_when_log_write_fails(
        self, app, db_session, monkeypatch,
    ):
        """If the ``ScheduleLog`` insert fails the CAS claim is reverted
        and the metric MUST NOT be incremented — matching on-disk
        reality (no log row was committed)."""
        from cms.services.device_manager import device_manager
        from cms import metrics

        calls: list[tuple] = []
        monkeypatch.setattr(
            metrics.scheduler_missed_emitted_total,
            "add",
            lambda *a, **kw: calls.append((a, kw)),
        )

        setting = CMSSetting(key="timezone", value="UTC")
        asset = Asset(
            filename="b2_revert.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum="b2revert1",
        )
        group = DeviceGroup(name="B2 Revert Group")
        device = Device(
            id="b2-revert-dev-01", name="B2 Revert Device",
            status=DeviceStatus.ADOPTED,
        )
        dummy = Device(
            id="b2-revert-dummy", name="B2 Revert Dummy",
            status=DeviceStatus.ADOPTED,
        )
        db_session.add_all([setting, asset, group, device, dummy])
        await db_session.flush()
        device.group_id = group.id

        sched = Schedule(
            name="B2 Revert Test",
            group_id=group.id,
            asset_id=asset.id,
            start_time=time(0, 0),
            end_time=time(23, 59, 59),
            priority=10,
            enabled=True,
        )
        db_session.add(sched)
        await db_session.commit()

        class FakeWS:
            async def send_json(self, data):
                pass

        device_manager.register("b2-revert-dummy", FakeWS())

        now = datetime.now(timezone.utc)
        db_session.add(ScheduleMissedEvent(
            schedule_id=sched.id,
            device_id="b2-revert-dev-01",
            occurrence_date=now.date(),
            first_seen_offline_at=now - timedelta(
                seconds=MISSED_GRACE_SECONDS + 5,
            ),
            emitted_at=None,
        ))
        await db_session.commit()

        # Force every ``ScheduleLog`` instantiation to raise so the
        # CAS-claim revert path is exercised.
        orig_init = ScheduleLog.__init__

        def _bad_init(self, *a, **kw):
            raise RuntimeError("simulated log insert failure")

        monkeypatch.setattr(ScheduleLog, "__init__", _bad_init)

        try:
            await evaluate_schedules()
        finally:
            monkeypatch.setattr(ScheduleLog, "__init__", orig_init)
            device_manager.disconnect("b2-revert-dummy")

        assert calls == [], (
            "When the log insert fails the CAS claim is reverted, so "
            "missed_emitted MUST stay at zero — counting it would "
            "lie about durable state"
        )

    async def test_missed_emitted_silent_when_no_missed_events(
        self, app, db_session, monkeypatch,
    ):
        """Tick with no MISSED events should not call ``.add(0)``.

        Suppressing the no-op call avoids a flat zero-valued series in
        App Insights which makes operator dashboards harder to scan.
        """
        from cms import metrics

        calls: list[tuple] = []
        monkeypatch.setattr(
            metrics.scheduler_missed_emitted_total,
            "add",
            lambda *a, **kw: calls.append((a, kw)),
        )

        # No schedules, no devices, no dedup rows — evaluate_schedules
        # is a fast no-op tick.
        await evaluate_schedules()

        assert calls == [], (
            "missed_emitted_total.add() should not be called on ticks "
            f"that emit zero MISSED events; got {calls!r}"
        )
