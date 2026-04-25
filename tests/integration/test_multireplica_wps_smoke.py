"""Multi-replica + WPS-transport smoke tests (issue #344, post-Stage-4).

These tests run only when the harness is brought up under the
``docker-compose.integration.wps.yml`` overlay (``--profile wps`` is
*not* what selects them — we use a separate compose file because
profiles can't override env on existing services). The
``multireplica-wps-smoke`` CI job sets
``AGORA_INTEGRATION_TRANSPORT=wps``; ``conftest.py`` reads that env
var and layers the WPS overlay onto the base compose file.

Scope is deliberately tight — these tests cover the cross-replica WPS
risks the existing ``-m smoke`` suite (which runs under direct-WS in
the other job) cannot, namely:

* ``test_connected_on_replica_a_is_visible_via_replica_b`` — POST a
  signed ``sys.connected`` directly at cms-0's ``/internal/wps/events``
  receiver; assert cms-1 sees the device as ``online=true`` with the
  correct ``connection_id``. Locks in the basic Stage 4 invariant: WPS
  presence writes by replica A are visible to replica B without any
  cache invalidation between them (post-#344 Stage 2c presence is in
  the ``devices`` table, not the per-replica ``device_manager``).

* ``test_stale_disconnect_after_reconnect_keeps_device_online`` — the
  scenario the CAS guard in ``mark_offline_and_alert(...
  expected_connection_id=...)`` was added to handle (issue #406): an
  old socket on replica A receives ``sys.disconnected`` *after* the
  device has already reconnected on replica B. POST signed
  ``sys.connected(conn=A)`` to cms-0, then ``sys.connected(conn=B)``
  to cms-1, then a stale ``sys.disconnected(conn=A)`` to cms-0; assert
  the device stays online with ``connection_id=B``. Without the CAS
  this would fire a bogus OFFLINE alert.

Why synthetic webhooks instead of a real broker round-trip:

* The local-broker fan-out is exercised by the *base* ``-m smoke``
  suite running under WPS in this same job (``cms-0`` is the broker's
  upstream). That suite already proves "WPS boot/auth/leader work".
* The cross-replica webhook race requires events for the same device
  to land on *different* replicas, which the broker (single
  ``WPS_UPSTREAM_URL``) cannot orchestrate. Direct POSTs are the
  smallest tool that exercises the path.
* No JWT minting or WSS plumbing — keeps the test code small and
  decoupled from broker internals so a refactor of the broker doesn't
  ripple into CI.

Future expansion (intentionally out of scope here): a real WSS device
fixture that connects to local-broker and asserts a REST ``:send`` from
the *opposite* replica reaches it. That tests broker behaviour more
than CMS multi-replica behaviour, so the cost/benefit isn't there for
a required gate. Track as a follow-up if/when broker behaviour starts
to drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

from shared.wps_signature import sign_connection_id

pytestmark = [pytest.mark.asyncio, pytest.mark.smoke_wps]

# Must match the value in ``docker-compose.integration.wps.yml`` —
# kept in sync intentionally because parameterising the overlay would
# add complexity for no benefit (the value is opaque test-only data).
WPS_ACCESS_KEY = "integration-wps-key"

CMS_A_URL = "http://127.0.0.1:8080"
CMS_B_URL = "http://127.0.0.1:8081"


def _ce_headers(
    *,
    ce_type: str,
    connection_id: str,
    user_id: str,
    event_name: str = "",
) -> dict[str, str]:
    """Build a CloudEvents-binary-binding header dict signed for our key.

    Mirrors what local-broker emits and what Azure WPS sends in prod.
    ``ce-id`` / ``ce-time`` are populated for completeness even though
    the receiver only checks ``ce-type``, ``ce-connectionId``, and the
    signature.
    """
    headers = {
        "ce-specversion": "1.0",
        "ce-type": ce_type,
        "ce-source": f"/hubs/agora/client/{connection_id}",
        "ce-id": uuid.uuid4().hex,
        "ce-time": datetime.now(timezone.utc).isoformat(),
        "ce-hub": "agora",
        "ce-connectionId": connection_id,
        "ce-userId": user_id,
        "ce-signature": sign_connection_id(connection_id, [WPS_ACCESS_KEY]),
        "Content-Type": "application/json",
    }
    if event_name:
        headers["ce-eventName"] = event_name
    return headers


async def _seed_minimal_device(engine: AsyncEngine) -> str:
    """Insert just a ``devices`` row with no group, no schedule.

    The WPS webhook ``sys.connected`` path doesn't read the group or
    asset; it only needs the device row to exist (well, actually
    ``mark_online`` issues an UPDATE that no-ops on a missing row, but
    the tests assert visibility against a real row so we still need
    one).
    """
    device_id = f"int-wps-{uuid.uuid4().hex[:10]}"
    now = datetime.now(timezone.utc)
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO devices "
                "(id, name, location, status, firmware_version, "
                " storage_capacity_mb, storage_used_mb, device_type, "
                " supported_codecs, registered_at, online, "
                " upgrade_started_at) "
                "VALUES (:id, :name, '', 'ADOPTED', '', 0, 0, '', '', "
                "        :now, FALSE, NULL)"
            ),
            {"id": device_id, "name": "wps-int-dev", "now": now},
        )
    return device_id


async def _cleanup(engine: AsyncEngine, device_id: str) -> None:
    async with engine.begin() as conn:
        await conn.execute(
            text("DELETE FROM devices WHERE id = :d"), {"d": device_id}
        )


async def _row(engine: AsyncEngine, device_id: str) -> dict[str, object]:
    """Read the freshest copy of the device row from the shared DB."""
    async with engine.connect() as conn:
        r = (
            await conn.execute(
                text(
                    "SELECT online, connection_id FROM devices WHERE id = :d"
                ),
                {"d": device_id},
            )
        ).first()
    assert r is not None, f"device {device_id} disappeared"
    return {"online": bool(r.online), "connection_id": r.connection_id}


async def test_connected_on_replica_a_is_visible_via_replica_b(
    engine: AsyncEngine,
) -> None:
    """A signed ``sys.connected`` POST to cms-0 must be visible on cms-1.

    Stage 4's invariant is that presence writes go through the shared
    Postgres row (``devices.online``, ``devices.connection_id``), so
    replica B sees them immediately without any cache message-passing.
    Without ``device_presence.mark_online`` (Stage 2c) this would have
    needed the long-since-removed in-memory ``device_manager``
    register-remote ghost entry on each replica.
    """
    device_id = await _seed_minimal_device(engine)
    connection_id = f"conn-a-{uuid.uuid4().hex[:8]}"
    try:
        # POST signed sys.connected directly at cms-0's webhook port.
        with httpx.Client(timeout=5.0) as c:
            r = c.post(
                f"{CMS_A_URL}/internal/wps/events",
                headers=_ce_headers(
                    ce_type="azure.webpubsub.sys.connected",
                    connection_id=connection_id,
                    user_id=device_id,
                ),
                content=b"{}",
            )
        assert r.status_code == 204, (
            f"sys.connected -> {r.status_code}: {r.text[:200]}"
        )

        # Read the row through the shared DB — what cms-1 would also
        # see when it serves /api/devices.
        state = await _row(engine, device_id)
        assert state["online"] is True
        assert state["connection_id"] == connection_id
    finally:
        await _cleanup(engine, device_id)


async def test_stale_disconnect_after_reconnect_keeps_device_online(
    engine: AsyncEngine,
) -> None:
    """CAS guard suppresses a stale disconnect from a replaced session.

    Reproduces the issue #406 scenario: an old socket on replica A
    receives ``sys.disconnected`` *after* the device has reconnected
    on replica B (under WPS in N>=2, both events arrive at the CMS
    fleet but for a brief window the old session's disconnect can be
    in flight while the new session's connect has already won). The
    CAS in ``mark_offline_and_alert(... expected_connection_id=A)``
    must refuse to flip the row offline because the live
    ``connection_id`` is now B.
    """
    device_id = await _seed_minimal_device(engine)
    conn_a = f"conn-a-{uuid.uuid4().hex[:8]}"
    conn_b = f"conn-b-{uuid.uuid4().hex[:8]}"
    try:
        with httpx.Client(timeout=5.0) as c:
            # 1. Original session connects, lands on replica A.
            r = c.post(
                f"{CMS_A_URL}/internal/wps/events",
                headers=_ce_headers(
                    ce_type="azure.webpubsub.sys.connected",
                    connection_id=conn_a,
                    user_id=device_id,
                ),
                content=b"{}",
            )
            assert r.status_code == 204, r.text[:200]

            # 2. Reconnect — new session lands on replica B.
            r = c.post(
                f"{CMS_B_URL}/internal/wps/events",
                headers=_ce_headers(
                    ce_type="azure.webpubsub.sys.connected",
                    connection_id=conn_b,
                    user_id=device_id,
                ),
                content=b"{}",
            )
            assert r.status_code == 204, r.text[:200]

            # Sanity: row should reflect the most recent connect (B).
            state = await _row(engine, device_id)
            assert state["online"] is True
            assert state["connection_id"] == conn_b

            # 3. Stale disconnect from the *original* session arrives —
            # could land on either replica; pick A to mirror the bug
            # report. Must NOT flip the row offline.
            r = c.post(
                f"{CMS_A_URL}/internal/wps/events",
                headers=_ce_headers(
                    ce_type="azure.webpubsub.sys.disconnected",
                    connection_id=conn_a,
                    user_id=device_id,
                ),
                content=b"{}",
            )
            assert r.status_code == 204, r.text[:200]

        state = await _row(engine, device_id)
        assert state["online"] is True, (
            "stale disconnect from old session flipped the row offline — "
            "CAS guard in mark_offline_and_alert is broken"
        )
        assert state["connection_id"] == conn_b, (
            "stale disconnect cleared connection_id — CAS guard left "
            "the row in an inconsistent state"
        )
    finally:
        await _cleanup(engine, device_id)
