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
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import hash_password
from cms.models.user import SETUP_TOKEN_TTL, Role, User, setup_token_is_expired


async def _get_role_id(db: AsyncSession, name: str) -> uuid.UUID:
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one().id


async def _create_pending_user(
    db: AsyncSession,
    *,
    email: str = "pending@test.com",
    setup_token: str = "test-setup-token-abc123",
    temp_password: str = "temp-password-1",
    setup_token_created_at: datetime | None = ...,  # type: ignore[assignment]
) -> User:
    role_id = await _get_role_id(db, "Viewer")
    # Default to a fresh issue timestamp so a just-created pending invite is
    # within its TTL. Pass an explicit value (including None) to simulate aged
    # or legacy tokens.
    if setup_token_created_at is ...:
        setup_token_created_at = datetime.now(timezone.utc)
    user = User(
        username=email.split("@")[0],
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password(temp_password),
        role_id=role_id,
        is_active=True,
        must_change_password=True,
        setup_token=setup_token,
        setup_token_created_at=setup_token_created_at,
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


async def _reload_last_login(db: AsyncSession, user_id: uuid.UUID):
    """Read last_login_at straight from the DB, bypassing any session cache."""
    result = await db.execute(
        select(User.last_login_at).where(User.id == user_id)
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
async def test_setup_account_get_does_not_set_last_login(
    app, unauthed_client, db_session
):
    """GET /setup-account must NOT stamp last_login_at; URL prefetchers
    (Defender Safe Links, Proofpoint, etc.) fire that GET before the user
    ever clicks, which previously produced phantom 'last login' timestamps
    on invitees who never actually completed setup."""
    user = await _create_pending_user(
        db_session, email="lastlogin1@test.com", setup_token="tok-ll-1"
    )
    user_id = user.id

    # Sanity: user has never logged in
    assert await _reload_last_login(db_session, user_id) is None

    resp = await unauthed_client.get(
        "/setup-account?token=tok-ll-1", follow_redirects=False
    )
    assert resp.status_code == 303, resp.text

    still_none = await _reload_last_login(db_session, user_id)
    assert still_none is None, (
        f"last_login_at must remain NULL after a prefetcher-style GET; got {still_none!r}"
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
async def test_force_password_change_sets_last_login(
    app, unauthed_client, db_session
):
    """Successfully completing /force-password-change is the first real
    authentication moment; last_login_at must be stamped here so the user
    actually shows up as 'has logged in' on the users page."""
    user = await _create_pending_user(
        db_session, email="ll-force@test.com", setup_token="tok-ll-force"
    )
    user_id = user.id

    assert await _reload_last_login(db_session, user_id) is None

    resp = await unauthed_client.get(
        "/setup-account?token=tok-ll-force", follow_redirects=False
    )
    assert resp.status_code == 303

    resp = await unauthed_client.post(
        "/force-password-change",
        data={"new_password": "set-via-form-1", "confirm_password": "set-via-form-1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    stamp = await _reload_last_login(db_session, user_id)
    assert stamp is not None, "last_login_at must be set after first successful setup"


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


@pytest.mark.asyncio
async def test_api_change_password_sets_last_login_on_setup_completion(
    app, unauthed_client, db_session
):
    """When a freshly-invited user finishes setup via the JSON API, the
    call doubles as their first authentication so last_login_at must be
    stamped (mirrors the form path)."""
    user = await _create_pending_user(
        db_session,
        email="ll-api@test.com",
        setup_token="tok-ll-api",
        temp_password="initial-temp-api",
    )
    user_id = user.id

    assert await _reload_last_login(db_session, user_id) is None

    resp = await unauthed_client.get(
        "/setup-account?token=tok-ll-api", follow_redirects=False
    )
    assert resp.status_code == 303

    resp = await unauthed_client.post(
        "/api/users/me/password",
        json={"current_password": "initial-temp-api", "new_password": "post-setup-1"},
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text

    stamp = await _reload_last_login(db_session, user_id)
    assert stamp is not None, "last_login_at must be set after first setup completion via API"


@pytest.mark.asyncio
async def test_api_change_password_preserves_last_login_for_normal_change(
    app, unauthed_client, db_session
):
    """A normal password change by an already-set-up user must NOT clobber
    last_login_at -- that field reflects the last real login, not the last
    password change. Without the must_change_password gate, this endpoint
    would overwrite a meaningful timestamp with a meaningless one."""
    from datetime import datetime, timezone, timedelta

    role_id = await _get_role_id(db_session, "Viewer")
    pinned = datetime.now(timezone.utc) - timedelta(days=7)
    user = User(
        username="settled",
        email="settled@test.com",
        display_name="Settled User",
        password_hash=hash_password("current-pwd-1"),
        role_id=role_id,
        is_active=True,
        must_change_password=False,
        setup_token=None,
        last_login_at=pinned,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    user_id = user.id

    # Log in to mint a session cookie (last_login_at will be refreshed by /login;
    # we capture the new value as the baseline we expect to be preserved).
    resp = await unauthed_client.post(
        "/login",
        data={"email": "settled@test.com", "password": "current-pwd-1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text
    baseline = await _reload_last_login(db_session, user_id)
    assert baseline is not None

    # Now change password via the API. last_login_at must NOT move.
    resp = await unauthed_client.post(
        "/api/users/me/password",
        json={"current_password": "current-pwd-1", "new_password": "next-pwd-2"},
        follow_redirects=False,
    )
    assert resp.status_code == 200, resp.text

    after = await _reload_last_login(db_session, user_id)
    assert after == baseline, (
        f"plain password change must not move last_login_at; baseline={baseline!r} after={after!r}"
    )


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


async def _reload_setup_token_created_at(db: AsyncSession, user_id: uuid.UUID):
    result = await db.execute(
        select(User.setup_token_created_at).where(User.id == user_id)
    )
    return result.scalar_one()


# -- setup-token TTL / expiry (issue #599) ---------------------------------


def test_setup_token_is_expired_predicate():
    """Unit-test the pure predicate: present-and-aged → expired; fresh →
    not; missing timestamp → fail closed; no token → not expired."""
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)

    fresh = User(setup_token="t", setup_token_created_at=now - timedelta(days=1))
    assert setup_token_is_expired(fresh, now=now) is False

    aged = User(
        setup_token="t",
        setup_token_created_at=now - (SETUP_TOKEN_TTL + timedelta(seconds=1)),
    )
    assert setup_token_is_expired(aged, now=now) is True

    # Exactly at the TTL boundary is still valid (strict ``>``).
    boundary = User(setup_token="t", setup_token_created_at=now - SETUP_TOKEN_TTL)
    assert setup_token_is_expired(boundary, now=now) is False

    # Token present but no issue timestamp → treated as expired (fail closed).
    no_ts = User(setup_token="t", setup_token_created_at=None)
    assert setup_token_is_expired(no_ts, now=now) is True

    # No token at all → nothing to validate, not "expired".
    none = User(setup_token=None, setup_token_created_at=None)
    assert setup_token_is_expired(none, now=now) is False


def test_setup_token_is_expired_tolerates_naive_timestamp():
    """SQLite (the CI unit matrix) returns naive datetimes; the predicate
    must assume UTC rather than raising on a naive/aware comparison."""
    now = datetime(2026, 6, 25, 12, 0, tzinfo=timezone.utc)
    naive_fresh = User(
        setup_token="t",
        setup_token_created_at=datetime(2026, 6, 24, 12, 0),  # naive, 1 day old
    )
    assert setup_token_is_expired(naive_fresh, now=now) is False


@pytest.mark.asyncio
async def test_setup_account_get_rejects_expired_token(
    app, unauthed_client, db_session
):
    """GET /setup-account with a token past its TTL must NOT log the user in;
    it returns the expired-link page (400) and leaves the token intact so the
    distinct message can keep being shown until an admin resends."""
    user = await _create_pending_user(
        db_session,
        email="expired1@test.com",
        setup_token="tok-expired-1",
        setup_token_created_at=datetime.now(timezone.utc)
        - (SETUP_TOKEN_TTL + timedelta(days=1)),
    )
    user_id = user.id

    resp = await unauthed_client.get(
        "/setup-account?token=tok-expired-1", follow_redirects=False
    )

    assert resp.status_code == 400, resp.text
    assert "expired" in resp.text.lower()
    # No session cookie minted.
    assert "set-cookie" not in {k.lower() for k in resp.headers}
    # Token untouched (not burned) by the failed GET.
    assert await _reload_setup_token(db_session, user_id) == "tok-expired-1"


@pytest.mark.asyncio
async def test_setup_account_get_accepts_token_within_ttl(
    app, unauthed_client, db_session
):
    """A token issued just inside the TTL window still works."""
    await _create_pending_user(
        db_session,
        email="fresh1@test.com",
        setup_token="tok-fresh-1",
        setup_token_created_at=datetime.now(timezone.utc)
        - (SETUP_TOKEN_TTL - timedelta(hours=1)),
    )

    resp = await unauthed_client.get(
        "/setup-account?token=tok-fresh-1", follow_redirects=False
    )
    assert resp.status_code == 303, resp.text
    assert resp.headers["location"] == "/force-password-change"


@pytest.mark.asyncio
async def test_setup_account_get_rejects_tokenless_timestamp(
    app, unauthed_client, db_session
):
    """A legacy row with a token but no issue timestamp fails closed (treated
    as expired) so a timestamp-less token can't grant indefinite access."""
    await _create_pending_user(
        db_session,
        email="legacy1@test.com",
        setup_token="tok-legacy-1",
        setup_token_created_at=None,
    )

    resp = await unauthed_client.get(
        "/setup-account?token=tok-legacy-1", follow_redirects=False
    )
    assert resp.status_code == 400, resp.text
    assert "expired" in resp.text.lower()


@pytest.mark.asyncio
async def test_force_password_change_clears_setup_token_created_at(
    app, unauthed_client, db_session
):
    """Completing setup burns the token AND clears its issue timestamp, so a
    finished row carries no stale TTL state."""
    user = await _create_pending_user(
        db_session, email="clearts1@test.com", setup_token="tok-clearts-1"
    )
    user_id = user.id

    resp = await unauthed_client.get(
        "/setup-account?token=tok-clearts-1", follow_redirects=False
    )
    assert resp.status_code == 303

    resp = await unauthed_client.post(
        "/force-password-change",
        data={"new_password": "brand-new-pw-1", "confirm_password": "brand-new-pw-1"},
        follow_redirects=False,
    )
    assert resp.status_code == 303, resp.text

    assert await _reload_setup_token(db_session, user_id) is None
    assert await _reload_setup_token_created_at(db_session, user_id) is None
