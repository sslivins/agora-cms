"""Unit tests for ``cms.services.leader``.

Both primitives have a fast no-op branch on non-Postgres and real
behaviour on Postgres.  Tests that exercise real semantics skip
automatically if the Postgres fixture isn't available (the standard
pattern in this test suite).
"""
from __future__ import annotations

import asyncio

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker

from cms.services.leader import LeaderLease, session_advisory_lock


# ── fixtures ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _wire_shared_database(db_engine):
    """Point ``shared.database`` globals at the test engine.

    ``cms.services.leader`` calls ``shared.database.get_engine()`` and
    ``get_session_factory()`` at runtime; without this, the engine is
    ``None`` and the non-Postgres no-op branch fires even on Postgres.
    """
    import shared.database as shared_db_mod

    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    prev_engine = shared_db_mod._engine
    prev_factory = shared_db_mod._session_factory
    shared_db_mod._engine = db_engine
    shared_db_mod._session_factory = factory
    try:
        yield
    finally:
        shared_db_mod._engine = prev_engine
        shared_db_mod._session_factory = prev_factory


# ── helpers ──────────────────────────────────────────────────────────

def _is_postgres(db_engine) -> bool:
    return db_engine.dialect.name == "postgresql"


def _skip_if_sqlite(db_engine) -> None:
    if not _is_postgres(db_engine):
        pytest.skip("requires postgres for real lease semantics")


async def _expire_lease_in_db(db_engine, loop_name: str) -> None:
    """Force a lease row into the past to simulate TTL expiry."""
    factory = async_sessionmaker(db_engine, expire_on_commit=False)
    async with factory() as session:
        await session.execute(
            text(
                "UPDATE leader_leases SET expires_at = NOW() - "
                "make_interval(secs => 10) WHERE loop_name = :n"
            ),
            {"n": loop_name},
        )
        await session.commit()


# ── LeaderLease ──────────────────────────────────────────────────────

class TestLeaderLeaseNonPostgres:
    """SQLite path: always reports leadership; start/stop no-op."""

    @pytest.mark.asyncio
    async def test_always_leader_on_sqlite(self, db_engine):
        if _is_postgres(db_engine):
            pytest.skip("sqlite-only")
        lease = LeaderLease("noop-test", ttl_s=30, heartbeat_s=10)
        await lease.start()
        assert lease.is_leader is True
        # No heartbeat task should have been spawned on sqlite.
        assert lease._task is None
        await lease.stop()
        assert lease.is_leader is True  # still "yes, you're the leader"


class TestLeaderLeaseValidation:
    def test_heartbeat_must_be_less_than_ttl(self):
        with pytest.raises(ValueError):
            LeaderLease("bad", ttl_s=10, heartbeat_s=10)
        with pytest.raises(ValueError):
            LeaderLease("bad", ttl_s=10, heartbeat_s=30)


class TestLeaderLeasePostgres:
    """Real lease semantics — require a Postgres-backed db_engine."""

    @pytest.mark.asyncio
    async def test_single_acquire(self, db_engine):
        _skip_if_sqlite(db_engine)
        lease = LeaderLease("solo", ttl_s=30, heartbeat_s=5)
        try:
            await lease.start()
            assert lease.is_leader is True
        finally:
            await lease.stop()

    @pytest.mark.asyncio
    async def test_concurrent_only_one_wins(self, db_engine):
        _skip_if_sqlite(db_engine)
        a = LeaderLease("concurrent", ttl_s=30, heartbeat_s=5)
        b = LeaderLease("concurrent", ttl_s=30, heartbeat_s=5)
        try:
            await a.start()
            await b.start()
            assert a.is_leader ^ b.is_leader, (
                f"exactly one should hold; a={a.is_leader} b={b.is_leader}"
            )
        finally:
            await a.stop()
            await b.stop()

    @pytest.mark.asyncio
    async def test_expiry_allows_takeover(self, db_engine):
        _skip_if_sqlite(db_engine)
        first = LeaderLease("takeover", ttl_s=30, heartbeat_s=5)
        second = LeaderLease("takeover", ttl_s=30, heartbeat_s=5)
        try:
            await first.start()
            assert first.is_leader is True
            # Simulate the holder hanging long enough for TTL to pass.
            await _expire_lease_in_db(db_engine, "takeover")
            await second.start()
            assert second.is_leader is True
        finally:
            await first.stop()
            await second.stop()

    @pytest.mark.asyncio
    async def test_release_on_stop_frees_lease(self, db_engine):
        _skip_if_sqlite(db_engine)
        holder = LeaderLease("release", ttl_s=60, heartbeat_s=5)
        await holder.start()
        assert holder.is_leader is True
        await holder.stop()
        # A fresh lease should be able to acquire immediately even
        # though the 60s TTL hasn't passed — stop() must have expired
        # the row.
        next_holder = LeaderLease("release", ttl_s=60, heartbeat_s=5)
        try:
            await next_holder.start()
            assert next_holder.is_leader is True
        finally:
            await next_holder.stop()

    @pytest.mark.asyncio
    async def test_heartbeat_renews_expires_at(self, db_engine):
        """After two heartbeat intervals, expires_at should move forward."""
        _skip_if_sqlite(db_engine)
        lease = LeaderLease("renew", ttl_s=5, heartbeat_s=1)
        factory = async_sessionmaker(db_engine, expire_on_commit=False)
        try:
            await lease.start()
            async with factory() as session:
                first = (
                    await session.execute(
                        text(
                            "SELECT expires_at FROM leader_leases "
                            "WHERE loop_name = 'renew'"
                        )
                    )
                ).scalar_one()
            await asyncio.sleep(2.5)  # ≥ 2 heartbeat intervals
            async with factory() as session:
                later = (
                    await session.execute(
                        text(
                            "SELECT expires_at FROM leader_leases "
                            "WHERE loop_name = 'renew'"
                        )
                    )
                ).scalar_one()
            assert later > first, (
                f"expected heartbeat to push expires_at forward; "
                f"first={first} later={later}"
            )
            assert lease.is_leader is True
        finally:
            await lease.stop()


# ── session_advisory_lock ────────────────────────────────────────────

class TestAdvisoryLockNonPostgres:
    @pytest.mark.asyncio
    async def test_always_granted_on_sqlite(self, db_engine):
        if _is_postgres(db_engine):
            pytest.skip("sqlite-only")
        async with session_advisory_lock(42) as got:
            assert got is True


class TestAdvisoryLockPostgres:
    @pytest.mark.asyncio
    async def test_single_acquire_releases_on_exit(self, db_engine):
        _skip_if_sqlite(db_engine)
        async with session_advisory_lock(991) as got:
            assert got is True
        # Should be reacquirable now that the previous scope released.
        async with session_advisory_lock(991) as got2:
            assert got2 is True

    @pytest.mark.asyncio
    async def test_concurrent_second_caller_blocked(self, db_engine):
        _skip_if_sqlite(db_engine)
        # Use an event to sequence the two would-be holders.
        b_may_try = asyncio.Event()
        a_done = asyncio.Event()
        results: dict[str, bool] = {}

        async def a():
            async with session_advisory_lock(992) as got:
                results["a"] = got
                b_may_try.set()
                await a_done.wait()

        async def b():
            await b_may_try.wait()
            async with session_advisory_lock(992) as got:
                results["b"] = got

        at = asyncio.create_task(a())
        bt = asyncio.create_task(b())
        await asyncio.wait_for(b_may_try.wait(), timeout=5)
        # Let b fully run its try + release before we let a release.
        await asyncio.sleep(0.2)
        await bt
        a_done.set()
        await at

        assert results["a"] is True, "a should hold the lock"
        assert results["b"] is False, "b should have been rejected"

    @pytest.mark.asyncio
    async def test_different_ids_do_not_collide(self, db_engine):
        _skip_if_sqlite(db_engine)
        async with session_advisory_lock(993) as a:
            async with session_advisory_lock(994) as b:
                assert a is True and b is True
