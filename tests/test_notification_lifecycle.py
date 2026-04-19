"""Notification lifecycle round-trip tests (#303).

Existing ``test_notifications.py`` covers per-endpoint behavior (list,
count, mark-read, delete) and visibility rules.  This file covers the
**round-trip** — chaining endpoints together to verify that client-facing
state (GET, GET /count, GET ?unread_only=true) stays consistent after
each mutation.

Why round-trip matters: in production an operator's flow is typically
``bell icon → open notification → ack → close bell``.  A regression that
breaks the second GET /count call (e.g. stale cache, wrong filter) only
surfaces end-to-end, not per-endpoint.

Snooze was requested in #303 but **no snooze endpoint exists today**.
``test_snooze_endpoint_not_implemented`` documents the gap so adding
snooze later requires wiring a real test.
"""

from __future__ import annotations

import uuid

import pytest

from tests.test_notifications import _create_notification, _create_user, _login_as


# ── Ack (mark-read) round-trip ──


@pytest.mark.asyncio
async def test_mark_read_propagates_to_count_and_filters(app, client, db_session):
    """POST /read must update GET /count AND GET ?unread_only=true."""
    n = await _create_notification(db_session, scope="system", title="Round-trip 1")

    r = await client.get("/api/notifications/count")
    assert r.status_code == 200
    assert r.json()["unread"] == 1

    r = await client.get("/api/notifications?unread_only=true")
    assert len(r.json()) == 1

    r = await client.post(f"/api/notifications/{n.id}/read")
    assert r.status_code == 200
    assert r.json()["read_at"] is not None

    # Count decremented
    r = await client.get("/api/notifications/count")
    assert r.json()["unread"] == 0

    # unread_only filter now excludes it
    r = await client.get("/api/notifications?unread_only=true")
    assert r.json() == []

    # Full list still includes it, with read_at set
    r = await client.get("/api/notifications")
    hits = [row for row in r.json() if row["id"] == str(n.id)]
    assert len(hits) == 1
    assert hits[0]["read_at"] is not None


@pytest.mark.asyncio
async def test_mark_read_is_idempotent(app, client, db_session):
    """POST /read twice must not mutate read_at the second time."""
    n = await _create_notification(db_session, scope="system", title="Idempotent")

    r1 = await client.post(f"/api/notifications/{n.id}/read")
    first_read_at = r1.json()["read_at"]
    assert first_read_at is not None

    r2 = await client.post(f"/api/notifications/{n.id}/read")
    assert r2.status_code == 200
    # Same read_at — router preserves it when already read.
    assert r2.json()["read_at"] == first_read_at


@pytest.mark.asyncio
async def test_read_all_zeroes_count_and_filter(app, client, db_session):
    """POST /read-all must mark every visible notification read at once."""
    for i in range(3):
        await _create_notification(db_session, scope="system", title=f"Bulk {i}")

    r = await client.get("/api/notifications/count")
    assert r.json()["unread"] == 3

    r = await client.post("/api/notifications/read-all")
    assert r.status_code == 200
    # Router returns {"marked_read": N} — at least our 3.
    assert r.json()["marked_read"] >= 3

    r = await client.get("/api/notifications/count")
    assert r.json()["unread"] == 0

    r = await client.get("/api/notifications?unread_only=true")
    assert r.json() == []


# ── Dismiss round-trip ──


@pytest.mark.asyncio
async def test_dismiss_removes_from_list_and_count(app, client, db_session):
    """DELETE /{id} must remove the notification from GET / and GET /count."""
    n = await _create_notification(db_session, scope="system", title="Dismiss me")

    r = await client.get("/api/notifications")
    assert any(row["id"] == str(n.id) for row in r.json())

    r = await client.delete(f"/api/notifications/{n.id}")
    assert r.status_code in (200, 204)

    r = await client.get("/api/notifications")
    assert not any(row["id"] == str(n.id) for row in r.json())

    r = await client.get("/api/notifications/count")
    assert r.json()["unread"] == 0


@pytest.mark.asyncio
async def test_dismiss_is_not_recoverable(app, client, db_session):
    """After DELETE, subsequent POST /read returns 404 (not a soft-delete)."""
    n = await _create_notification(db_session, scope="system", title="Gone")

    await client.delete(f"/api/notifications/{n.id}")
    r = await client.post(f"/api/notifications/{n.id}/read")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_dismiss_a_read_notification_is_fine(app, client, db_session):
    """Read-then-delete lifecycle: ack first, then dismiss, no state-transition errors."""
    n = await _create_notification(db_session, scope="system", title="Read then gone")

    r = await client.post(f"/api/notifications/{n.id}/read")
    assert r.status_code == 200

    r = await client.delete(f"/api/notifications/{n.id}")
    assert r.status_code in (200, 204)

    r = await client.get("/api/notifications/count")
    assert r.json()["unread"] == 0


@pytest.mark.asyncio
async def test_delete_all_clears_list(app, client, db_session):
    """DELETE / must wipe every visible notification."""
    for i in range(3):
        await _create_notification(db_session, scope="system", title=f"Wipe {i}")

    r = await client.get("/api/notifications")
    assert len([row for row in r.json() if row["title"].startswith("Wipe")]) == 3

    r = await client.delete("/api/notifications")
    assert r.status_code in (200, 204)

    r = await client.get("/api/notifications")
    # None of our Wipe-* rows remain (other tests may have injected extras;
    # we only assert our fixtures are gone).
    assert not any(row["title"].startswith("Wipe") for row in r.json())


# ── Visibility-preserving lifecycle ──


@pytest.mark.asyncio
async def test_read_all_does_not_touch_invisible_notifications(app, db_session):
    """A user's POST /read-all must not mark OTHER users' notifications read.

    RBAC invariant across the lifecycle: read-all is scoped to what the
    caller can see.
    """
    alice = await _create_user(db_session, email="alice@example.com", role_name="Viewer")
    bob = await _create_user(db_session, email="bob@example.com", role_name="Viewer")

    # User-scoped notifications: only the owner sees theirs.
    alice_notif = await _create_notification(
        db_session, scope="user", user_id=alice.id, title="For Alice"
    )
    bob_notif = await _create_notification(
        db_session, scope="user", user_id=bob.id, title="For Bob"
    )

    ac = await _login_as(app, "alice@example.com")
    try:
        r = await ac.post("/api/notifications/read-all")
        assert r.status_code == 200
    finally:
        await ac.aclose()

    # Reload both notifications fresh.
    await db_session.refresh(alice_notif)
    await db_session.refresh(bob_notif)
    assert alice_notif.read_at is not None, "Alice's notification should be marked read"
    assert bob_notif.read_at is None, (
        "Bob's notification must NOT be marked read by Alice's read-all"
    )


@pytest.mark.asyncio
async def test_dismiss_all_does_not_touch_invisible_notifications(app, db_session):
    """DELETE / is scoped — one user's clear-all must not dismiss another user's."""
    alice = await _create_user(db_session, email="alice2@example.com", role_name="Viewer")
    bob = await _create_user(db_session, email="bob2@example.com", role_name="Viewer")

    await _create_notification(
        db_session, scope="user", user_id=alice.id, title="Alice only"
    )
    bob_notif = await _create_notification(
        db_session, scope="user", user_id=bob.id, title="Bob only"
    )

    ac = await _login_as(app, "alice2@example.com")
    try:
        await ac.delete("/api/notifications")
    finally:
        await ac.aclose()

    # Bob's notification must still exist.
    bc = await _login_as(app, "bob2@example.com")
    try:
        r = await bc.get("/api/notifications")
        assert any(row["id"] == str(bob_notif.id) for row in r.json()), (
            "Bob's notification was erroneously dismissed by Alice"
        )
    finally:
        await bc.aclose()


# ── Error-path round-trip ──


@pytest.mark.asyncio
async def test_mark_unknown_notification_returns_404(client):
    """Ack of a non-existent UUID must not fabricate a notification."""
    fake = uuid.uuid4()
    r = await client.post(f"/api/notifications/{fake}/read")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_dismiss_unknown_notification_returns_404(client):
    fake = uuid.uuid4()
    r = await client.delete(f"/api/notifications/{fake}")
    assert r.status_code == 404


# ── Snooze gap ──


@pytest.mark.asyncio
async def test_snooze_endpoint_not_implemented(app, client, db_session):
    """#303 asked for snooze — verify it's still missing so adding it is
    intentional, not accidental.

    Snooze would look like ``POST /api/notifications/{id}/snooze`` with a
    duration payload.  When implemented, replace this test with a
    positive-path lifecycle test and update #303's "covered by" in the
    commit.
    """
    n = await _create_notification(db_session, scope="system", title="Snooze?")
    r = await client.post(
        f"/api/notifications/{n.id}/snooze",
        json={"seconds": 600},
    )
    assert r.status_code in (404, 405), (
        f"snooze endpoint appears to exist (status {r.status_code}); "
        "update this test to a real lifecycle check and close the #303 gap properly"
    )
