"""Behavioural tests for leader-lease presence telemetry (Pillar B).

These tests verify that ``cms.services.leader.LeaderLease`` emits the
expected :class:`opentelemetry.metrics.Counter` ``.add()`` calls when
its lease state machine transitions between leader / non-leader.

Strategy mirrors ``tests/test_scheduler_metrics.py``:

* monkeypatch the counter handles' ``.add()`` methods so we capture
  exact call arguments without coupling to the OTel SDK;
* drive the heartbeat loop deterministically by patching
  ``cms.services.leader.asyncio.sleep`` to return once and then raise
  :class:`asyncio.CancelledError`, giving us exactly one body
  iteration per test;
* override ``_try_acquire`` on the instance so we don't need a real
  Postgres backend.

Coverage:

* ``start()`` first-acquire success → exactly one ``claim``;
* ``start()`` first-acquire failure → no emission;
* heartbeat F→T flip → ``claim``;
* heartbeat T→F flip → ``claim_lost``;
* heartbeat raises → ``heartbeat_late`` (and ``claim_lost`` on the
  T→F transition that the exception forces);
* non-Postgres backend → no emission whatsoever (the heartbeat is
  never spawned and there is no real state machine).
"""

from __future__ import annotations

import asyncio
import types

import pytest

from cms.services.leader import LeaderLease


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


class _FakePgEngine:
    """Minimal stand-in for an SQLAlchemy AsyncEngine on Postgres."""

    class _Dialect:
        name = "postgresql"

    dialect = _Dialect()


def _capture_add(monkeypatch, handle_name: str) -> list[tuple]:
    """Patch ``cms.metrics.<handle_name>.add`` to record calls.

    Returns the list that will be appended to.
    """
    from cms import metrics

    calls: list[tuple] = []
    handle = getattr(metrics, handle_name)
    monkeypatch.setattr(
        handle,
        "add",
        lambda *a, **kw: calls.append((a, kw)),
    )
    return calls


def _install_one_tick_sleep(monkeypatch) -> None:
    """Make ``asyncio.sleep`` (as seen by leader.py) return once then cancel.

    The heartbeat structure is::

        while True:
            await asyncio.sleep(...)   # sleep #1: returns
            <body runs>                # one full iteration
            <loop top>
            await asyncio.sleep(...)   # sleep #2: raises CancelledError

    This gives us exactly one body iteration before the loop exits.
    """
    import cms.services.leader as leader_mod

    state = {"n": 0}

    async def _fake_sleep(_):
        state["n"] += 1
        if state["n"] >= 2:
            raise asyncio.CancelledError()
        return None

    monkeypatch.setattr(leader_mod.asyncio, "sleep", _fake_sleep)


def _bind_try_acquire(lease: LeaderLease, fn) -> None:
    """Replace ``lease._try_acquire`` with the given async function."""
    lease._try_acquire = types.MethodType(  # type: ignore[method-assign]
        lambda self: fn(), lease,
    )


# ----------------------------------------------------------------------
# start() — first-acquire emission
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_emits_claim_on_first_acquire_success(monkeypatch):
    from cms import metrics

    monkeypatch.setattr(
        "cms.services.leader._get_engine", lambda: _FakePgEngine(),
    )

    claim_calls = _capture_add(monkeypatch, "presence_claim_total")
    claim_lost_calls = _capture_add(monkeypatch, "presence_claim_lost_total")
    heartbeat_late_calls = _capture_add(
        monkeypatch, "presence_heartbeat_late_total"
    )

    # Stop the heartbeat from doing real work after start() spawns it.
    _install_one_tick_sleep(monkeypatch)

    lease = LeaderLease("scheduler", ttl_s=30, heartbeat_s=5)

    async def _acquire_true() -> bool:
        return True

    _bind_try_acquire(lease, _acquire_true)
    # Bypass DB on shutdown.
    lease._release = types.MethodType(  # type: ignore[method-assign]
        lambda self: _noop_async(), lease,
    )

    await lease.start()
    await lease.stop()

    assert len(claim_calls) == 1, claim_calls
    amount, attrs = claim_calls[0][0]
    assert amount == 1
    assert attrs == {metrics.ATTR_LOOP_NAME: "scheduler"}
    assert claim_lost_calls == []
    assert heartbeat_late_calls == []


@pytest.mark.asyncio
async def test_start_no_emission_on_first_acquire_failure(monkeypatch):
    monkeypatch.setattr(
        "cms.services.leader._get_engine", lambda: _FakePgEngine(),
    )

    claim_calls = _capture_add(monkeypatch, "presence_claim_total")
    claim_lost_calls = _capture_add(monkeypatch, "presence_claim_lost_total")
    heartbeat_late_calls = _capture_add(
        monkeypatch, "presence_heartbeat_late_total"
    )

    _install_one_tick_sleep(monkeypatch)

    lease = LeaderLease("scheduler", ttl_s=30, heartbeat_s=5)

    async def _acquire_false() -> bool:
        return False

    _bind_try_acquire(lease, _acquire_false)
    lease._release = types.MethodType(  # type: ignore[method-assign]
        lambda self: _noop_async(), lease,
    )

    await lease.start()
    await lease.stop()

    # Initial state was False, first acquire returned False — no flip.
    assert claim_calls == []
    assert claim_lost_calls == []
    assert heartbeat_late_calls == []


# ----------------------------------------------------------------------
# Heartbeat — state transitions
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_heartbeat_emits_claim_on_false_to_true(monkeypatch):
    from cms import metrics

    claim_calls = _capture_add(monkeypatch, "presence_claim_total")
    claim_lost_calls = _capture_add(monkeypatch, "presence_claim_lost_total")
    heartbeat_late_calls = _capture_add(
        monkeypatch, "presence_heartbeat_late_total"
    )

    _install_one_tick_sleep(monkeypatch)

    lease = LeaderLease("alert_sweep", ttl_s=30, heartbeat_s=5)
    lease._enabled = True
    lease._is_leader = False  # initial state for this tick

    async def _acquire_true() -> bool:
        return True

    _bind_try_acquire(lease, _acquire_true)

    with pytest.raises(asyncio.CancelledError):
        await lease._heartbeat_run()

    assert len(claim_calls) == 1
    amount, attrs = claim_calls[0][0]
    assert amount == 1
    assert attrs == {metrics.ATTR_LOOP_NAME: "alert_sweep"}
    assert claim_lost_calls == []
    assert heartbeat_late_calls == []
    assert lease._is_leader is True


@pytest.mark.asyncio
async def test_heartbeat_emits_claim_lost_on_true_to_false(monkeypatch):
    from cms import metrics

    claim_calls = _capture_add(monkeypatch, "presence_claim_total")
    claim_lost_calls = _capture_add(monkeypatch, "presence_claim_lost_total")
    heartbeat_late_calls = _capture_add(
        monkeypatch, "presence_heartbeat_late_total"
    )

    _install_one_tick_sleep(monkeypatch)

    lease = LeaderLease("scheduler", ttl_s=30, heartbeat_s=5)
    lease._enabled = True
    lease._is_leader = True  # we were the leader at the start of this tick

    async def _acquire_false() -> bool:
        return False

    _bind_try_acquire(lease, _acquire_false)

    with pytest.raises(asyncio.CancelledError):
        await lease._heartbeat_run()

    assert claim_calls == []
    assert len(claim_lost_calls) == 1
    amount, attrs = claim_lost_calls[0][0]
    assert amount == 1
    assert attrs == {metrics.ATTR_LOOP_NAME: "scheduler"}
    assert heartbeat_late_calls == []
    assert lease._is_leader is False


@pytest.mark.asyncio
async def test_heartbeat_emits_late_on_exception(monkeypatch):
    from cms import metrics

    claim_calls = _capture_add(monkeypatch, "presence_claim_total")
    claim_lost_calls = _capture_add(monkeypatch, "presence_claim_lost_total")
    heartbeat_late_calls = _capture_add(
        monkeypatch, "presence_heartbeat_late_total"
    )

    _install_one_tick_sleep(monkeypatch)

    lease = LeaderLease("scheduler", ttl_s=30, heartbeat_s=5)
    lease._enabled = True
    lease._is_leader = True  # was leader; the exception forces F state

    async def _boom() -> bool:
        raise RuntimeError("simulated DB outage")

    _bind_try_acquire(lease, _boom)

    with pytest.raises(asyncio.CancelledError):
        await lease._heartbeat_run()

    # The exception bumps heartbeat_late, and because the resulting
    # state is False (default after exception) we *also* see
    # claim_lost for the involuntary T→F transition.  This is the
    # documented behaviour: heartbeat_late is the "why" and
    # claim_lost is the "what".
    assert len(heartbeat_late_calls) == 1
    amount, attrs = heartbeat_late_calls[0][0]
    assert amount == 1
    assert attrs == {metrics.ATTR_LOOP_NAME: "scheduler"}
    assert claim_calls == []
    assert len(claim_lost_calls) == 1
    assert lease._is_leader is False


# ----------------------------------------------------------------------
# Non-Postgres backend — heartbeat skipped, no emission
# ----------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_emission_on_non_postgres_backend(monkeypatch):
    # ``_get_engine`` returns None (sqlite/no-DB scenario in tests).
    monkeypatch.setattr("cms.services.leader._get_engine", lambda: None)

    claim_calls = _capture_add(monkeypatch, "presence_claim_total")
    claim_lost_calls = _capture_add(monkeypatch, "presence_claim_lost_total")
    heartbeat_late_calls = _capture_add(
        monkeypatch, "presence_heartbeat_late_total"
    )

    lease = LeaderLease("scheduler", ttl_s=30, heartbeat_s=5)
    await lease.start()

    # SQLite-fallback path always reports leadership but never emits.
    assert lease.is_leader is True
    assert lease._enabled is False
    assert lease._task is None
    assert claim_calls == []
    assert claim_lost_calls == []
    assert heartbeat_late_calls == []

    await lease.stop()
    assert claim_calls == []
    assert claim_lost_calls == []
    assert heartbeat_late_calls == []


# ----------------------------------------------------------------------
# Helpers (placed at end so test fns above read top-down)
# ----------------------------------------------------------------------


async def _noop_async() -> None:
    return None
