"""Regression tests for deferring setup_token nullification.

The bug fixed alongside these tests: ``GET /setup-account?token=...`` used
to null ``setup_token`` on first hit, making the URL strictly single-use.
Outbound mail leaving an M365 tenant goes through Defender for Office 365
Safe Links, which DETONATES URLs in transit to evaluate reputation; that
detonation is a real HEAD then GET against the URL. Result: the token was
burned ~3 seconds after user creation, before the human ever saw the email.
External recipients (i.e. anyone outside the sending tenant) could never
complete setup.

Fix: defer the token-burn to the moment the user actually completes setup
(successful POST to ``/force-password-change`` or ``/api/users/me/password``).
The GET is now idempotent and replay-safe.

Closing the latent gap that the deferral surfaced: ``/api/users`` was the
only API router without ``require_auth`` at the router level, so a half-set
user (one who'd hit the GET but not yet set a password) could call
``/api/users/me`` and ``/api/users/me/password`` directly. With the longer
window the new behaviour creates, that's a real exploit -- so this PR also
gates ``/api/users`` on ``require_auth`` and exempts the password-change
endpoint specifically from the ``must_change_password`` redirect.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import hash_password
from cms.models.user import Role, User


async def _get_role_id(db: AsyncSession, name: str) -> uuid.UUID:
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one().id


async def _create_pending_user(
    db: AsyncSession,
    *,
    email: str = "pending@test.com",
    setup_token: str = "test-setup-token-abc123",
    temp_password: str = "temp-password-1",
) -> User:
    role_id = await _get_role_id(db, "Viewer")
    user = User(
        username=email.split("@")[0],
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password(temp_password),
        role_id=role_id,
        is_active=True,
        must_change_password=True,
        setup_token=setup_token,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _reload_setup_token(db: AsyncSession, user_id: uuid.UUID) -> str | None:
    """Read setup_token straight from the DB, bypassing any session cache.

    Tests that mutate state through the HTTP client see those mutations via
    a different DB session; our local db_session may have a stale view of
    the row. ``db.execute(select(...))`` always issues a fresh query, so we
    just need to make sure the user_id we filter on is a plain UUID (not a
    ``user.id`` ORM attribute that would trigger lazy reload).
    """
    result = await db.execute(select(User.setup_token).where(User.id == user_id))
    return result.scalar_one()


async def _reload_must_change(db: AsyncSession, user_id: uuid.UUID) -> bool:
    result = await db.execute(
        select(User.must_change_password).where(User.id == user_id)
    )
    return result.scalar_one()


# -- token deferral on GET ------------------------------------------------


@pytest.mark.asyncio
async def test_setup_account_get_does_not_burn_token(
    app, unauthed_client, db_session
):
    """GET /setup-account?token=X must NOT null setup_token; the prefetch
    fix depends on this so an outbound URL-detonator can't kill the link."""
    user = await _create_pending_user(
        db_session, email="defer1@test.com", setup_token="tok-defer-1"
    )
    user_id = user.id

    resp = await unauthed_client.get(
        "/setup-account?token=tok-defer-1", follow_redirects=False
    )

    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/force-password-change"

    still_set = await _reload_setup_token(db_session, user_id)
    assert still_set == "tok-defer-1", (
        f"setup_token must survive GET; got {still_set!r}"
    )


@pytest.mark.asyncio
async def test_setup_account_get_is_idempotent(app, unauthed_client, db_session):
    """A second GET with the same token must also succeed -- defeats every
    URL prefetcher that does HEAD+GET (Safe Links, Proofpoint, etc.) and
    also any legit retry from the user (back button, refresh)."""
    await _create_pending_user(
        db_session, email="defer2@test.com", setup_token="tok-defer-2"
    )

    first = await unauthed_client.get(
        "/setup-account?token=tok-defer-2", follow_redirects=False
    )
    assert first.status_code == 303, first.text

    # Drop the cookie the first GET set so the second GET looks like a
    # fresh prefetch from a different client (i.e. the Defender detonator
    # followed by the human user from their email).
    unauthed_client.cookies.clear()

    second = await unauthed_client.get(
        "/setup-account?token=tok-defer-2", follow_redirects=False
    )
    assert second.status_code == 303, second.text
    assert second.headers["location"] == "/force-password-change"


# -- token burned on actual setup completion -------------------------------


@pytest.mark.asyncio
async def test_force_password_change_burns_setup_token(
    app, unauthed_client, db_session
):
    """POST /force-password-change with a valid password must null setup_token.
    This is the only moment we know the human actually completed setup."""
    user = await _create_pending_user(
        db_session, email="burn1@test.com", setup_token="tok-burn-1"
    )
    user_id = user.id

    # Click the magic link to get a session cookie.
    resp = await unauthed_client.get(
        "/setup-account?token=tok-burn-1", follow_redirects=False
    )
    assert resp.status_code == 303

    # Submit a new password via the form.
    resp = await unauthed_client.post(
        "/force-password-change",
        data={"new_password": "new-password-456", "confirm_password": "new-password-456"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    assert await _reload_setup_token(db_session, user_id) is None
    assert await _reload_must_change(db_session, user_id) is False


@pytest.mark.asyncio
async def test_api_change_password_burns_setup_token(
    app, unauthed_client, db_session
):
    """POST /api/users/me/password with the temp password must null setup_token."""
    user = await _create_pending_user(
        db_session,
        email="burn2@test.com",
        setup_token="tok-burn-2",
        temp_password="initial-temp-1",
    )
    user_id = user.id

    # Get a cookie via the magic link.
    resp = await unauthed_client.get(
        "/setup-account?token=tok-burn-2", follow_redirects=False
    )
    assert resp.status_code == 303

    # Change password through the JSON API the way a JS client would.
    resp = await unauthed_client.post(
        "/api/users/me/password",
        json={"current_password": "initial-temp-1", "new_password": "new-password-789"},
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text

    assert await _reload_setup_token(db_session, user_id) is None
    assert await _reload_must_change(db_session, user_id) is False


# -- /api/users hardening: must_change_password gate -----------------------


@pytest.mark.asyncio
async def test_must_change_password_user_blocked_from_get_me(
    app, unauthed_client, db_session
):
    """A half-set user (must_change_password=True) with a session cookie
    must NOT be able to read /api/users/me; require_auth's exempt-set
    intentionally does not include it. Pre-fix this leaked profile data
    (email, role, group_ids, permissions) to anyone holding the URL."""
    await _create_pending_user(
        db_session, email="gate1@test.com", setup_token="tok-gate-1"
    )
    resp = await unauthed_client.get(
        "/setup-account?token=tok-gate-1", follow_redirects=False
    )
    assert resp.status_code == 303

    resp = await unauthed_client.get("/api/users/me", follow_redirects=False)
    assert resp.status_code == 307, resp.text
    assert resp.headers["location"] == "/force-password-change"


@pytest.mark.asyncio
async def test_must_change_password_user_blocked_from_users_list(
    app, unauthed_client, db_session
):
    """Same gate must cover every other /api/users route -- they all gain
    the must_change_password check via the router-level dependency."""
    await _create_pending_user(
        db_session, email="gate2@test.com", setup_token="tok-gate-2"
    )
    resp = await unauthed_client.get(
        "/setup-account?token=tok-gate-2", follow_redirects=False
    )
    assert resp.status_code == 303

    resp = await unauthed_client.get("/api/users", follow_redirects=False)
    assert resp.status_code == 307, resp.text


@pytest.mark.asyncio
async def test_must_change_password_user_can_hit_password_endpoint(
    app, unauthed_client, db_session
):
    """The password-change endpoint MUST stay reachable mid-setup -- it's
    the whole point of the flow. If require_auth's exempt-set drops it,
    users get trapped in a redirect loop and can never finish setup."""
    user = await _create_pending_user(
        db_session,
        email="gate3@test.com",
        setup_token="tok-gate-3",
        temp_password="initial-temp-3",
    )
    user_id = user.id
    resp = await unauthed_client.get(
        "/setup-account?token=tok-gate-3", follow_redirects=False
    )
    assert resp.status_code == 303

    resp = await unauthed_client.post(
        "/api/users/me/password",
        json={"current_password": "initial-temp-3", "new_password": "fresh-password-3"},
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text

    assert await _reload_must_change(db_session, user_id) is False


@pytest.mark.asyncio
async def test_password_change_with_wrong_current_password_does_not_burn_token(
    app, unauthed_client, db_session
):
    """If the password-change call fails (wrong current_password), the
    setup_token must NOT be nulled -- otherwise an attacker with the URL
    could brick the link by submitting bogus password attempts."""
    user = await _create_pending_user(
        db_session,
        email="burn3@test.com",
        setup_token="tok-burn-3",
        temp_password="initial-temp-3b",
    )
    user_id = user.id
    resp = await unauthed_client.get(
        "/setup-account?token=tok-burn-3", follow_redirects=False
    )
    assert resp.status_code == 303

    resp = await unauthed_client.post(
        "/api/users/me/password",
        json={"current_password": "wrong-password", "new_password": "anything-456"},
        follow_redirects=False,
    )
    assert resp.status_code == 400, resp.text

    assert await _reload_setup_token(db_session, user_id) == "tok-burn-3"
    assert await _reload_must_change(db_session, user_id) is True
