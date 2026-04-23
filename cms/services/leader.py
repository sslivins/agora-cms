"""Leader-election primitives for multi-replica CMS loops.

Two flavours, picked by blast radius:

* :class:`LeaderLease` — durable, table-backed lease with background
  heartbeat.  Intended for loops where **bounded-time failover matters**
  (scheduler, service-key rotation).  If the holder dies silently, a
  standby replica takes over within ``ttl_s`` regardless of whether
  the holder's TCP connection has been reaped.
* :func:`session_advisory_lock` — cheap opportunistic session-scoped
  advisory lock pinned to a dedicated ``AsyncConnection``.  Intended
  for idempotent housekeeping (media probe, deleted-asset reaper,
  device-purge) where "two replicas did it, one wasted I/O" is the
  worst case.

Both degrade gracefully on non-Postgres: the lease reports
``is_leader=True`` unconditionally and the advisory-lock context
manager yields ``True``.  Unit tests run on SQLite without needing the
Postgres-only primitives.

Design notes:

* Advisory locks live on a **connection** — SQLAlchemy's pool returns
  connections on ``commit()``, so the lock must be held on an explicit
  ``engine.connect()`` pinned across the scope.  A xact-scoped lock
  would release the moment the caller commits any unrelated work.
* The lease's heartbeat runs on its own task so long ticks (large
  fleets, multi-second DB calls) don't starve renewal.  Pick
  ``ttl_s`` ≳ 3 × ``heartbeat_s`` so a single skipped heartbeat
  doesn't drop the lease.
* Release is best-effort.  If the process is killed between `stop()`
  being called and the release ``UPDATE`` landing, the lease simply
  expires via TTL — same as a hard crash.  Callers never need to
  retry release.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from contextlib import asynccontextmanager
from typing import AsyncIterator, Optional

from sqlalchemy import text

log = logging.getLogger("agora.leader")


def _get_engine():
    """Import-at-call so test monkeypatches of ``_engine`` take effect."""
    from shared.database import get_engine

    return get_engine()


def _get_session_factory():
    from shared.database import get_session_factory

    return get_session_factory()


class LeaderLease:
    """Table-backed leader lease with async heartbeat.

    Usage from a long-running loop::

        lease = LeaderLease("scheduler", ttl_s=30, heartbeat_s=10)
        try:
            await lease.start()
            while not stop_event.is_set():
                if not lease.is_leader:
                    await asyncio.sleep(5)
                    continue
                await do_work()
        finally:
            await lease.stop()

    On non-Postgres databases (i.e. SQLite in unit tests), the lease is
    disabled and :attr:`is_leader` always returns ``True``; ``start`` /
    ``stop`` are cheap no-ops.  This keeps the loop code identical
    across environments.
    """

    def __init__(
        self,
        loop_name: str,
        *,
        ttl_s: int = 30,
        heartbeat_s: int = 10,
        holder_id: Optional[str] = None,
    ) -> None:
        if heartbeat_s >= ttl_s:
            raise ValueError(
                f"heartbeat_s ({heartbeat_s}) must be < ttl_s ({ttl_s}); "
                "rule of thumb ttl_s = 3 × heartbeat_s"
            )
        self.loop_name = loop_name
        self.ttl_s = ttl_s
        self.heartbeat_s = heartbeat_s
        # Per-instance holder id.  Surviving process identity isn't
        # interesting; what matters is that two replicas can't collide.
        self.holder_id = holder_id or str(uuid.uuid4())
        self._task: Optional[asyncio.Task] = None
        self._is_leader: bool = False
        # Postgres-gated at start(); default True so tests that never
        # call start() (e.g. construct-and-inspect) still observe the
        # fallback "always leader" semantics.
        self._enabled: bool = True

    # ── public API ──────────────────────────────────────────────────

    @property
    def is_leader(self) -> bool:
        """Current belief about leadership.

        On non-Postgres, always ``True``.  On Postgres, reflects the
        result of the most recent heartbeat.  Between heartbeats the
        flag is intentionally **stale** — a lease holder that crashes
        mid-tick will report ``True`` until the next heartbeat, and a
        lease takeover observed on replica B will only flip this to
        ``False`` on the next heartbeat.  Callers that care about
        strict correctness should not gate write paths on this flag;
        it is for scheduling, not mutual exclusion.
        """

        if not self._enabled:
            return True
        return self._is_leader

    async def start(self) -> None:
        """Begin the heartbeat task.

        First try_acquire is awaited synchronously so :attr:`is_leader`
        is up to date by the time ``start()`` returns — callers can
        branch on leadership immediately without a first-tick delay.

        Idempotent: a second call while the heartbeat task is already
        running is a no-op.
        """

        if self._task is not None:
            return

        engine = _get_engine()
        if engine is None or engine.dialect.name != "postgresql":
            self._enabled = False
            log.info(
                "LeaderLease[%s] non-postgres backend; always-leader",
                self.loop_name,
            )
            return

        # Synchronous first acquire so the caller can branch immediately.
        try:
            self._is_leader = await self._try_acquire()
            log.info(
                "LeaderLease[%s] start: is_leader=%s holder=%s",
                self.loop_name, self._is_leader, self.holder_id,
            )
        except Exception as e:
            log.warning(
                "LeaderLease[%s] initial acquire failed: %s (will retry)",
                self.loop_name, e,
            )
            self._is_leader = False

        self._task = asyncio.create_task(
            self._heartbeat_run(), name=f"leader-lease:{self.loop_name}"
        )

    async def stop(self) -> None:
        """Cancel the heartbeat and best-effort release the lease.

        Safe to call multiple times and safe to call without a prior
        ``start``.  Release errors are logged and swallowed — the lease
        will expire via TTL.
        """

        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None
        await self._release()

    # ── internals ───────────────────────────────────────────────────

    async def _heartbeat_run(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.heartbeat_s)
                try:
                    new_state = await self._try_acquire()
                except Exception as e:
                    log.warning(
                        "LeaderLease[%s] heartbeat failed: %s",
                        self.loop_name, e,
                    )
                    new_state = False
                if new_state != self._is_leader:
                    log.info(
                        "LeaderLease[%s] state change: is_leader=%s",
                        self.loop_name, new_state,
                    )
                self._is_leader = new_state
        except asyncio.CancelledError:
            raise

    async def _try_acquire(self) -> bool:
        """Attempt to insert-or-renew the lease row.

        Semantics: we win if (a) no row exists, (b) the row's
        ``expires_at`` is in the past, or (c) we already hold it.
        Implemented as ``INSERT ... ON CONFLICT DO UPDATE ... WHERE``;
        the ``RETURNING holder_id`` tells us who won.  When the
        conditional update is filtered out (someone else holds a live
        lease), no row is returned — failure.
        """

        factory = _get_session_factory()
        async with factory() as session:
            result = await session.execute(
                text(
                    """
                    INSERT INTO leader_leases
                        (loop_name, holder_id, expires_at, renewed_at)
                    VALUES
                        (:loop_name, :holder_id,
                         NOW() + make_interval(secs => :ttl),
                         NOW())
                    ON CONFLICT (loop_name) DO UPDATE
                      SET holder_id = EXCLUDED.holder_id,
                          expires_at = EXCLUDED.expires_at,
                          renewed_at = EXCLUDED.renewed_at
                      WHERE leader_leases.expires_at < NOW()
                         OR leader_leases.holder_id = :holder_id
                    RETURNING holder_id
                    """
                ),
                {
                    "loop_name": self.loop_name,
                    "holder_id": self.holder_id,
                    "ttl": self.ttl_s,
                },
            )
            row = result.first()
            await session.commit()
            return row is not None and row[0] == self.holder_id

    async def _release(self) -> None:
        """Best-effort: mark our lease row expired.

        Intentionally NOT gated on ``self._is_leader`` — that flag is
        only our belief at the last heartbeat, and a transient DB
        error between heartbeats could leave us owning a live row
        while ``_is_leader`` is ``False``.  The ``WHERE holder_id =
        :holder_id`` makes the UPDATE safe against stomping a
        successor: if somebody else took the lease, their holder_id
        won't match and the UPDATE touches nothing.
        """

        if not self._enabled:
            self._is_leader = False
            return
        try:
            factory = _get_session_factory()
            async with factory() as session:
                # Mark our row expired; only our own row to avoid stomping
                # a successor that grabbed the lease in the meantime.
                await session.execute(
                    text(
                        """
                        UPDATE leader_leases
                           SET expires_at = NOW()
                         WHERE loop_name = :loop_name
                           AND holder_id = :holder_id
                        """
                    ),
                    {
                        "loop_name": self.loop_name,
                        "holder_id": self.holder_id,
                    },
                )
                await session.commit()
        except Exception as e:
            log.warning(
                "LeaderLease[%s] release failed (lease will TTL out): %s",
                self.loop_name, e,
            )
        finally:
            self._is_leader = False


@asynccontextmanager
async def session_advisory_lock(lock_id: int) -> AsyncIterator[bool]:
    """Try ``pg_try_advisory_lock`` on a pinned ``AsyncConnection``.

    Yields ``True`` iff we acquired the lock; the connection is held
    for the full scope of the ``with`` block and released on exit.
    Yields ``False`` and does no I/O further if the lock is already
    held by some other replica.

    On non-Postgres, always yields ``True`` — unit tests get the
    "yes, you're the leader" branch without needing the real DB.

    Intended for idempotent housekeeping loops where the cost of two
    replicas doing the same pass is "a bit of wasted I/O", not
    corruption.  For loops where bounded-time failover matters, use
    :class:`LeaderLease` instead.
    """

    engine = _get_engine()
    if engine is None or engine.dialect.name != "postgresql":
        yield True
        return

    # AUTOCOMMIT so the connection does not sit "idle in transaction"
    # for the whole ``with`` block — that would hold snapshots open,
    # interfere with VACUUM, and create needless DB pressure on the
    # long-running housekeeping loops this primitive is designed for.
    # Session-scoped advisory locks survive commit/rollback, so we
    # don't need a transaction wrapping them.
    async with engine.connect() as raw_conn:
        conn = await raw_conn.execution_options(isolation_level="AUTOCOMMIT")
        result = await conn.execute(
            text("SELECT pg_try_advisory_lock(:id)"),
            {"id": lock_id},
        )
        got = bool(result.scalar_one())
        try:
            yield got
        finally:
            if got:
                try:
                    await conn.execute(
                        text("SELECT pg_advisory_unlock(:id)"),
                        {"id": lock_id},
                    )
                except Exception as e:
                    # If unlock fails the connection will be returned
                    # in a broken state; the pool's pre-ping + recycle
                    # will reap it.  Losing an unlock temporarily just
                    # means the backend holds the lock until the
                    # connection is reaped, which is acceptable for
                    # opportunistic housekeeping.
                    log.warning(
                        "advisory_unlock(%s) failed: %s", lock_id, e,
                    )
