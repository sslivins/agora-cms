"""Multi-replica smoke tests — required-gated subset.

Lightweight cross-replica smoke checks that gate every PR via the
``ci-gate`` umbrella. These tests catch catastrophic multi-replica
regressions (broken leader election, split-brain, replicas that can
boot but not serve requests, etc.) on the PR before they land.

Scope is deliberately narrow:

* **``test_both_replicas_serve_authenticated_request``** — asserts
  each replica returns 200 to an authenticated ``/api/devices`` GET
  after login. Catches the "replica boots and answers /healthz but
  crashes on first real request" class of bug — we've shipped that
  regression once before (Alembic migration race) and the MVP tests
  wouldn't have caught it because they only exercise their own seeded
  state.

* **``test_exactly_one_scheduler_and_offline_sweep_leader``** — reads
  ``leader_leases`` and asserts that for each critical loop
  (``scheduler`` + ``offline_sweep``) **exactly one** replica has a
  live lease. Offline-sweep is on the "high-temp alert never missed"
  critical path — if that loop double-runs we generate duplicate
  alerts; if it doesn't run at all we miss them.

Heavier assertions (skip propagation, concurrent-upgrade CAS) live in
``test_multireplica_mvp.py`` — marked ``smoke`` too so they run in
the same job, but they carry the bulk of the cross-replica
state-coherence coverage.
"""

from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

import httpx

pytestmark = [pytest.mark.asyncio, pytest.mark.smoke]

# Loops we require a single-owner lease for before considering the
# multi-replica cluster healthy. Both are safety-critical:
#
# * ``scheduler`` — drives config pushes + skip + MISSED detection
# * ``offline_sweep`` — drives the device-offline alert path
#
# Other leader-gated loops (``service_key_rotation``, ``device_purge``,
# ``deleted_asset_reaper``) are either low-frequency or idempotent and
# not worth paying CI time for on every PR.
CRITICAL_LOOPS = ("scheduler", "offline_sweep")


async def test_both_replicas_serve_authenticated_request(
    client_a: httpx.Client, client_b: httpx.Client
) -> None:
    """Both replicas handle an authenticated read path, not just /healthz."""
    for label, client in (("A", client_a), ("B", client_b)):
        r = client.get("/api/devices")
        assert r.status_code == 200, f"replica {label} /api/devices -> {r.status_code}: {r.text[:200]}"
        # Sanity: response is JSON-parseable (catches middleware
        # regressions that serve HTML instead).
        assert r.headers.get("content-type", "").startswith("application/json"), (
            f"replica {label} /api/devices content-type: {r.headers.get('content-type')!r}"
        )


async def test_exactly_one_scheduler_and_offline_sweep_leader(
    engine: AsyncEngine,
) -> None:
    """Critical loops must have exactly one **stable** live leader.

    ``offline_sweep`` sleeps 30 s at startup before even attempting to
    acquire its lease (see ``cms/main.py``), so we need a generous
    window just to *see* the row. On top of that, a brief startup
    split-brain (both replicas briefly holding a lease while one
    expires) would self-heal — we want to catch it, not be robbed by
    returning on the first clean sample.

    Strategy: poll up to ~90 s looking for **3 consecutive clean
    samples** spaced ~1 s apart for every loop in ``CRITICAL_LOOPS``.
    Exactly-one + stability over 3 s is a strong invariant; a
    transient doubled-lease, a flapping election, or a never-acquired
    lease all fail this.
    """
    import asyncio

    total_deadline = 90.0
    interval = 1.0
    required_clean_streak = 3

    elapsed = 0.0
    streak = 0
    counts: dict[str, int] = {}
    errors: list[str] = []
    while elapsed < total_deadline:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT loop_name, COUNT(*) "
                        "FROM leader_leases "
                        "WHERE expires_at > NOW() "
                        "GROUP BY loop_name"
                    )
                )
            ).all()
        counts = {loop: int(count) for loop, count in rows}
        errors = [
            f"{loop}: expected 1 live leader, got {counts.get(loop, 0)}"
            for loop in CRITICAL_LOOPS
            if counts.get(loop, 0) != 1
        ]
        if errors:
            streak = 0
        else:
            streak += 1
            if streak >= required_clean_streak:
                return
        await asyncio.sleep(interval)
        elapsed += interval

    assert not errors, (
        "; ".join(errors)
        + f" (full counts: {counts}, streak: {streak}/{required_clean_streak})"
    )
