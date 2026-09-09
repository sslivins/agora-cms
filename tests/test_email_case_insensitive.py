"""Email addresses must be treated case-insensitively.

Reported in production: an account created as ``Mia.Amaranto@evergreengoodwill.org``
could not sign in as ``mia.amaranto@evergreengoodwill.org`` -- the audit log
recorded ``user_not_found``.

Per RFC 5321 the domain part is case-insensitive, and while the local part is
technically case-sensitive the same RFC warns against exploiting that. Every
mainstream provider treats the whole address case-insensitively, and so should
we.

Two halves to the contract:

* **Lookup** -- login, forgot-password and the uniqueness guards must match
  regardless of case, including for rows written before this change.
* **Storage** -- new writes normalise to lowercase, so a plain ``==`` in some
  future query cannot reintroduce the bug.
"""

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import hash_password
from cms.models.audit_log import AuditLog
from cms.models.user import Role, User

MIXED = "Mia.Amaranto@evergreengoodwill.org"
LOWER = "mia.amaranto@evergreengoodwill.org"
UPPER = "MIA.AMARANTO@EVERGREENGOODWILL.ORG"
PASSWORD = "correct-horse-1"


async def _role_id(db: AsyncSession, name: str = "Viewer") -> uuid.UUID:
    return (await db.execute(select(Role).where(Role.name == name))).scalar_one().id


async def _make_user(db: AsyncSession, email: str, *, username: str | None = None) -> User:
    """Insert a user with ``email`` stored verbatim.

    Deliberately bypasses the API so we can plant a mixed-case row exactly as
    it exists in the production database today.
    """
    user = User(
        username=username or email.split("@")[0],
        email=email,
        display_name="Mia",
        password_hash=hash_password(PASSWORD),
        role_id=await _role_id(db),
        is_active=True,
        must_change_password=False,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


# ── Login ──


@pytest.mark.asyncio
class TestLoginIsCaseInsensitive:
    @pytest.mark.parametrize("typed", [LOWER, UPPER, MIXED])
    async def test_stored_mixed_case_accepts_any_casing(
        self, unauthed_client, db_session, typed
    ):
        """The exact production report."""
        await _make_user(db_session, MIXED)

        resp = await unauthed_client.post(
            "/login",
            data={"username": typed, "password": PASSWORD},
            follow_redirects=False,
        )
        assert resp.status_code == 303, (
            f"login as {typed!r} against a stored {MIXED!r} was rejected"
        )
        assert "agora_cms_session" in resp.cookies

    async def test_stored_lowercase_accepts_mixed_case(self, unauthed_client, db_session):
        await _make_user(db_session, LOWER)

        resp = await unauthed_client.post(
            "/login",
            data={"username": MIXED, "password": PASSWORD},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "agora_cms_session" in resp.cookies

    async def test_username_login_is_case_insensitive(self, unauthed_client, db_session):
        """Usernames are derived from the email local part, so they inherit
        whatever casing the address had and hit the identical trap."""
        await _make_user(db_session, MIXED, username="Mia.Amaranto")

        resp = await unauthed_client.post(
            "/login",
            data={"username": "mia.amaranto", "password": PASSWORD},
            follow_redirects=False,
        )
        assert resp.status_code == 303
        assert "agora_cms_session" in resp.cookies

    async def test_wrong_password_still_rejected(self, unauthed_client, db_session):
        """Case-insensitivity must not weaken authentication itself."""
        await _make_user(db_session, MIXED)

        resp = await unauthed_client.post(
            "/login",
            data={"username": LOWER, "password": "not-the-password"},
            follow_redirects=False,
        )
        assert resp.status_code == 401
        assert "agora_cms_session" not in resp.cookies

    async def test_genuinely_unknown_user_still_audits_not_found(
        self, unauthed_client, db_session
    ):
        await _make_user(db_session, MIXED)

        resp = await unauthed_client.post(
            "/login",
            data={"username": "someone.else@evergreengoodwill.org", "password": PASSWORD},
            follow_redirects=False,
        )
        assert resp.status_code == 401

        rows = (
            await db_session.execute(
                select(AuditLog).where(AuditLog.action == "auth.login_failed")
            )
        ).scalars().all()
        assert [r.details["reason"] for r in rows] == ["user_not_found"]


# ── Forgot password ──


@pytest.mark.asyncio
class TestForgotPasswordIsCaseInsensitive:
    async def test_reset_matches_regardless_of_case(
        self, unauthed_client, db_session, monkeypatch
    ):
        """This path fails *silently* -- the anti-enumeration page is identical
        whether or not a user matched -- so a case mismatch gives the operator
        no feedback at all."""
        import cms.services.email_service as email_mod

        monkeypatch.setattr(
            email_mod, "send_password_reset_email_background", lambda **kw: None
        )
        from cms.auth import SETTING_SMTP_FROM_EMAIL, SETTING_SMTP_HOST, set_setting

        await set_setting(db_session, SETTING_SMTP_HOST, "smtp.test")
        await set_setting(db_session, SETTING_SMTP_FROM_EMAIL, "cms@test")
        await db_session.commit()

        user = await _make_user(db_session, MIXED)

        resp = await unauthed_client.post("/forgot-password", data={"email": LOWER})
        assert resp.status_code == 200

        await db_session.refresh(user)
        assert user.reset_token is not None, (
            "a reset requested with different casing minted no token, and the "
            "generic response means the operator never finds out"
        )


# ── Storage normalisation ──


@pytest.mark.asyncio
class TestEmailsAreStoredLowercase:
    async def test_create_user_normalises(self, client, db_session):
        from cms.auth import SETTING_SMTP_FROM_EMAIL, SETTING_SMTP_HOST, set_setting

        await set_setting(db_session, SETTING_SMTP_HOST, "smtp.test")
        await set_setting(db_session, SETTING_SMTP_FROM_EMAIL, "cms@test")
        await db_session.commit()

        resp = await client.post(
            "/api/users",
            json={
                "email": "New.Person@Example.COM",
                "display_name": "New Person",
                "role_id": str(await _role_id(db_session)),
            },
        )
        assert resp.status_code in (200, 201), resp.text
        assert resp.json()["email"] == "new.person@example.com"

        row = (
            await db_session.execute(
                select(User).where(User.email == "new.person@example.com")
            )
        ).scalar_one_or_none()
        assert row is not None, "the stored address was not normalised to lowercase"
        assert row.username == "new.person", (
            "the username is derived from the email local part and must be "
            "normalised alongside it"
        )

    async def test_duplicate_differing_only_by_case_is_rejected(
        self, client, db_session
    ):
        from cms.auth import SETTING_SMTP_FROM_EMAIL, SETTING_SMTP_HOST, set_setting

        await set_setting(db_session, SETTING_SMTP_HOST, "smtp.test")
        await set_setting(db_session, SETTING_SMTP_FROM_EMAIL, "cms@test")
        await db_session.commit()
        role_id = str(await _role_id(db_session))

        first = await client.post(
            "/api/users",
            json={"email": "dupe@example.com", "display_name": "A", "role_id": role_id},
        )
        assert first.status_code in (200, 201), first.text

        second = await client.post(
            "/api/users",
            json={"email": "DUPE@example.com", "display_name": "B", "role_id": role_id},
        )
        assert second.status_code == 409, (
            "two accounts differing only by case must not both exist -- "
            f"got {second.status_code}: {second.text}"
        )
