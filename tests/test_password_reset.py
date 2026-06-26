"""Tests for the forgot-password / self-service reset flow (issue #231).

Covers the TTL predicate, the anti-enumeration ``/forgot-password`` behaviour,
the deferred (Safe-Links-safe) ``/reset-password`` GET, the password-setting
POST, and the offline CLI fallback's argument handling.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import (
    SETTING_SMTP_FROM_EMAIL,
    SETTING_SMTP_HOST,
    hash_password,
    set_setting,
    verify_password,
)
from cms.models.user import (
    RESET_TOKEN_TTL,
    Role,
    User,
    reset_token_is_expired,
)


async def _get_role_id(db: AsyncSession, name: str) -> uuid.UUID:
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one().id


async def _create_user(
    db: AsyncSession,
    *,
    email: str = "reset-target@test.com",
    password: str = "old-password-1",
    is_active: bool = True,
    reset_token: str | None = None,
    reset_token_created_at: datetime | None = None,
) -> User:
    role_id = await _get_role_id(db, "Viewer")
    user = User(
        username=email.split("@")[0],
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password(password),
        role_id=role_id,
        is_active=is_active,
        must_change_password=False,
        reset_token=reset_token,
        reset_token_created_at=reset_token_created_at,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _configure_smtp(db: AsyncSession) -> None:
    await set_setting(db, SETTING_SMTP_HOST, "smtp.test.local")
    await set_setting(db, SETTING_SMTP_FROM_EMAIL, "noreply@test.local")
    await db.commit()


async def _reload(db: AsyncSession, user_id: uuid.UUID) -> User:
    # Expire the identity map so attribute access re-reads from the DB —
    # the HTTP request committed via a *different* session, so our cached
    # instance would otherwise show stale (pre-request) values.
    db.expire_all()
    result = await db.execute(select(User).where(User.id == user_id))
    return result.scalar_one()


# -- TTL predicate --------------------------------------------------------


def _user_with_reset(token, created_at):
    return User(reset_token=token, reset_token_created_at=created_at)


def test_reset_token_fresh_is_not_expired():
    now = datetime.now(timezone.utc)
    u = _user_with_reset("tok", now - timedelta(minutes=5))
    assert reset_token_is_expired(u, now=now) is False


def test_reset_token_aged_is_expired():
    now = datetime.now(timezone.utc)
    u = _user_with_reset("tok", now - (RESET_TOKEN_TTL + timedelta(minutes=1)))
    assert reset_token_is_expired(u, now=now) is True


def test_reset_token_absent_is_not_expired():
    u = _user_with_reset(None, None)
    assert reset_token_is_expired(u) is False


def test_reset_token_without_timestamp_fails_closed():
    u = _user_with_reset("tok", None)
    assert reset_token_is_expired(u) is True


def test_reset_token_naive_timestamp_assumed_utc():
    now = datetime.now(timezone.utc)
    naive = (now - timedelta(minutes=5)).replace(tzinfo=None)
    u = _user_with_reset("tok", naive)
    assert reset_token_is_expired(u, now=now) is False


# -- /forgot-password (anti-enumeration) ----------------------------------


@pytest.mark.asyncio
async def test_forgot_password_unknown_email_is_generic(unauthed_client, db_session):
    await _configure_smtp(db_session)
    resp = await unauthed_client.post(
        "/forgot-password", data={"email": "nobody@test.com"}
    )
    assert resp.status_code == 200
    assert "sent a link to reset" in resp.text


@pytest.mark.asyncio
async def test_forgot_password_known_user_mints_token(
    unauthed_client, db_session, monkeypatch
):
    import cms.services.email_service as email_mod

    sent = {}

    def _fake_send(**kwargs):
        sent.update(kwargs)

    monkeypatch.setattr(email_mod, "send_password_reset_email_background", _fake_send)

    await _configure_smtp(db_session)
    user = await _create_user(db_session, email="known@test.com")

    resp = await unauthed_client.post(
        "/forgot-password", data={"email": "known@test.com"}
    )
    assert resp.status_code == 200
    assert "sent a link to reset" in resp.text

    reloaded = await _reload(db_session, user.id)
    assert reloaded.reset_token is not None
    assert reloaded.reset_token_created_at is not None


@pytest.mark.asyncio
async def test_forgot_password_no_smtp_does_not_mint_token(unauthed_client, db_session):
    # SMTP deliberately NOT configured.
    user = await _create_user(db_session, email="nosmtp@test.com")
    resp = await unauthed_client.post(
        "/forgot-password", data={"email": "nosmtp@test.com"}
    )
    assert resp.status_code == 200
    assert "sent a link to reset" in resp.text

    reloaded = await _reload(db_session, user.id)
    assert reloaded.reset_token is None


@pytest.mark.asyncio
async def test_forgot_password_inactive_user_does_not_mint_token(
    unauthed_client, db_session
):
    await _configure_smtp(db_session)
    user = await _create_user(
        db_session, email="inactive@test.com", is_active=False
    )
    resp = await unauthed_client.post(
        "/forgot-password", data={"email": "inactive@test.com"}
    )
    assert resp.status_code == 200
    reloaded = await _reload(db_session, user.id)
    assert reloaded.reset_token is None


# -- GET /reset-password (deferred burn) ----------------------------------


@pytest.mark.asyncio
async def test_reset_password_get_valid_shows_form(unauthed_client, db_session):
    now = datetime.now(timezone.utc)
    await _create_user(
        db_session,
        email="getvalid@test.com",
        reset_token="reset-tok-getvalid",
        reset_token_created_at=now,
    )
    resp = await unauthed_client.get("/reset-password?token=reset-tok-getvalid")
    assert resp.status_code == 200
    assert "Set a New Password" in resp.text
    assert "reset-tok-getvalid" in resp.text


@pytest.mark.asyncio
async def test_reset_password_get_does_not_burn_token(unauthed_client, db_session):
    """A Safe-Links / Proofpoint prefetch GET must not consume the token."""
    now = datetime.now(timezone.utc)
    user = await _create_user(
        db_session,
        email="getnoburn@test.com",
        reset_token="reset-tok-noburn",
        reset_token_created_at=now,
    )
    await unauthed_client.get("/reset-password?token=reset-tok-noburn")
    reloaded = await _reload(db_session, user.id)
    assert reloaded.reset_token == "reset-tok-noburn"


@pytest.mark.asyncio
async def test_reset_password_get_invalid_token_rejected(unauthed_client, db_session):
    resp = await unauthed_client.get("/reset-password?token=does-not-exist")
    assert resp.status_code == 400
    assert "invalid or has expired" in resp.text


@pytest.mark.asyncio
async def test_reset_password_get_expired_token_rejected(unauthed_client, db_session):
    old = datetime.now(timezone.utc) - (RESET_TOKEN_TTL + timedelta(minutes=1))
    await _create_user(
        db_session,
        email="getexpired@test.com",
        reset_token="reset-tok-expired",
        reset_token_created_at=old,
    )
    resp = await unauthed_client.get("/reset-password?token=reset-tok-expired")
    assert resp.status_code == 400
    assert "invalid or has expired" in resp.text


# -- POST /reset-password -------------------------------------------------


@pytest.mark.asyncio
async def test_reset_password_post_sets_password_and_burns_token(
    unauthed_client, db_session
):
    now = datetime.now(timezone.utc)
    user = await _create_user(
        db_session,
        email="postok@test.com",
        password="old-password-1",
        reset_token="reset-tok-postok",
        reset_token_created_at=now,
    )
    resp = await unauthed_client.post(
        "/reset-password",
        data={
            "token": "reset-tok-postok",
            "new_password": "brand-new-pw",
            "confirm_password": "brand-new-pw",
        },
    )
    assert resp.status_code == 200
    assert "password has been reset" in resp.text

    reloaded = await _reload(db_session, user.id)
    assert reloaded.reset_token is None
    assert reloaded.reset_token_created_at is None
    assert reloaded.must_change_password is False
    assert verify_password("brand-new-pw", reloaded.password_hash)
    assert not verify_password("old-password-1", reloaded.password_hash)


@pytest.mark.asyncio
async def test_reset_password_post_expired_token_rejected(unauthed_client, db_session):
    old = datetime.now(timezone.utc) - (RESET_TOKEN_TTL + timedelta(minutes=1))
    user = await _create_user(
        db_session,
        email="postexpired@test.com",
        password="old-password-1",
        reset_token="reset-tok-postexpired",
        reset_token_created_at=old,
    )
    resp = await unauthed_client.post(
        "/reset-password",
        data={
            "token": "reset-tok-postexpired",
            "new_password": "brand-new-pw",
            "confirm_password": "brand-new-pw",
        },
    )
    assert resp.status_code == 400
    reloaded = await _reload(db_session, user.id)
    assert verify_password("old-password-1", reloaded.password_hash)


@pytest.mark.asyncio
async def test_reset_password_post_mismatch_rejected(unauthed_client, db_session):
    now = datetime.now(timezone.utc)
    user = await _create_user(
        db_session,
        email="postmismatch@test.com",
        reset_token="reset-tok-mismatch",
        reset_token_created_at=now,
    )
    resp = await unauthed_client.post(
        "/reset-password",
        data={
            "token": "reset-tok-mismatch",
            "new_password": "brand-new-pw",
            "confirm_password": "different-pw",
        },
    )
    assert resp.status_code == 400
    assert "do not match" in resp.text
    # Token must survive a failed attempt so the user can retry.
    reloaded = await _reload(db_session, user.id)
    assert reloaded.reset_token == "reset-tok-mismatch"


@pytest.mark.asyncio
async def test_reset_password_post_too_short_rejected(unauthed_client, db_session):
    now = datetime.now(timezone.utc)
    await _create_user(
        db_session,
        email="postshort@test.com",
        reset_token="reset-tok-short",
        reset_token_created_at=now,
    )
    resp = await unauthed_client.post(
        "/reset-password",
        data={
            "token": "reset-tok-short",
            "new_password": "x",
            "confirm_password": "x",
        },
    )
    assert resp.status_code == 400
    assert "at least 6 characters" in resp.text


# -- login page surfaces the entry point ----------------------------------


@pytest.mark.asyncio
async def test_login_page_has_forgot_link(unauthed_client):
    resp = await unauthed_client.get("/login")
    assert resp.status_code == 200
    assert "/forgot-password" in resp.text


# -- CLI fallback argument handling ---------------------------------------


def test_cli_generate_produces_password():
    import argparse

    from cms.__main__ import _resolve_new_password

    ns = argparse.Namespace(password=None, generate=True)
    pw = _resolve_new_password(ns)
    assert len(pw) >= 6


def test_cli_explicit_password_too_short_exits():
    import argparse

    from cms.__main__ import _resolve_new_password

    ns = argparse.Namespace(password="x", generate=False)
    with pytest.raises(SystemExit):
        _resolve_new_password(ns)


def test_cli_explicit_password_passthrough():
    import argparse

    from cms.__main__ import _resolve_new_password

    ns = argparse.Namespace(password="a-good-password", generate=False)
    assert _resolve_new_password(ns) == "a-good-password"

