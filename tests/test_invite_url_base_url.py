"""Regression test for the welcome-email setup URL.

The bug fixed alongside this test: the URL emitted in invite emails was the
Azure Container App default-domain FQDN instead of the bound custom domain
(``agora.egnw.org`` on Goodwill, ``agora.mennlabs.com`` on prod). The
router already had the right logic --

    base = (get_settings().base_url or request.base_url._url).rstrip("/")
    setup_url = f"{base}/setup-account?token={token}"

-- but ``AGORA_CMS_BASE_URL`` was hardcoded by bicep to the Azure default
domain, so ``get_settings().base_url`` always won and always pointed at the
wrong host. The bicep fix exposes a ``cmsBaseUrlOverride`` knob set from the
``CMS_CUSTOM_DOMAIN`` GitHub Actions variable per environment; this test
guards the router contract so a future regression that flips back to
``request.base_url`` (or drops the env var) is caught immediately.
"""

import uuid
from contextlib import asynccontextmanager
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession

from cms import auth as auth_module
from cms.auth import hash_password, set_setting
from cms.models.user import Role, User
from cms.services import email_service as email_service_module


# ── Helpers (mirroring tests/test_notifications.py) ──


async def _get_role_id(db: AsyncSession, name: str) -> uuid.UUID:
    from sqlalchemy import select
    result = await db.execute(select(Role).where(Role.name == name))
    return result.scalar_one().id


async def _setup_smtp(db: AsyncSession) -> None:
    await set_setting(db, "smtp_host", "smtp.example.com")
    await set_setting(db, "smtp_from_email", "noreply@example.com")
    await db.commit()


async def _create_pending_user(
    db: AsyncSession, *, email: str = "pending@test.com"
) -> User:
    role_id = await _get_role_id(db, "Viewer")
    user = User(
        username=email.split("@")[0],
        email=email,
        display_name=email.split("@")[0],
        password_hash=hash_password("temp-password"),
        role_id=role_id,
        is_active=True,
        must_change_password=True,
        setup_token="initial-token",
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@asynccontextmanager
async def _capture_welcome_setup_url(monkeypatch: pytest.MonkeyPatch):
    """Patch send_welcome_email_background to capture the setup_url it gets."""
    captured: dict[str, Any] = {}

    def _capture(**kwargs: Any) -> None:
        captured.update(kwargs)

    monkeypatch.setattr(
        email_service_module, "send_welcome_email_background", _capture
    )
    yield captured


def _set_base_url_env(monkeypatch: pytest.MonkeyPatch, value: str | None) -> None:
    """Set/clear AGORA_CMS_BASE_URL and bust the Settings lru_cache so the
    router picks up the new value on its next get_settings() call."""
    if value is None:
        monkeypatch.delenv("AGORA_CMS_BASE_URL", raising=False)
    else:
        monkeypatch.setenv("AGORA_CMS_BASE_URL", value)
    auth_module.get_settings.cache_clear()


@pytest_asyncio.fixture(autouse=True)
async def _restore_settings_cache():
    """Ensure other tests aren't poisoned by a cached Settings carrying our
    monkeypatched env var."""
    yield
    auth_module.get_settings.cache_clear()


# ── create user ──


@pytest.mark.asyncio
async def test_create_user_setup_url_uses_base_url_override(
    app, client, db_session, monkeypatch
):
    """When AGORA_CMS_BASE_URL is set, the invite link uses it -- not the
    request host."""
    await _setup_smtp(db_session)
    role_id = await _get_role_id(db_session, "Viewer")

    _set_base_url_env(monkeypatch, "https://agora.egnw.org")

    async with _capture_welcome_setup_url(monkeypatch) as captured:
        resp = await client.post(
            "/api/users",
            json={
                "email": "newcomer@test.com",
                "display_name": "Newcomer",
                "role_id": str(role_id),
                "group_ids": [],
            },
        )
        assert resp.status_code == 201, resp.text

    setup_url = captured.get("setup_url")
    assert setup_url, f"send_welcome_email_background was never called; captured={captured!r}"
    assert setup_url.startswith("https://agora.egnw.org/setup-account?token="), (
        f"expected custom-domain prefix, got {setup_url!r}"
    )


@pytest.mark.asyncio
async def test_create_user_setup_url_falls_back_to_request_host_when_unset(
    app, client, db_session, monkeypatch
):
    """When AGORA_CMS_BASE_URL is unset, the link falls back to the request
    host so local dev / non-customised deploys still work."""
    await _setup_smtp(db_session)
    role_id = await _get_role_id(db_session, "Viewer")

    _set_base_url_env(monkeypatch, None)

    async with _capture_welcome_setup_url(monkeypatch) as captured:
        resp = await client.post(
            "/api/users",
            json={
                "email": "fallback@test.com",
                "display_name": "Fallback",
                "role_id": str(role_id),
                "group_ids": [],
            },
        )
        assert resp.status_code == 201, resp.text

    setup_url = captured.get("setup_url")
    assert setup_url, "send_welcome_email_background was never called"
    assert setup_url.startswith("http://test/setup-account?token="), (
        f"expected fallback to test client host, got {setup_url!r}"
    )


# ── resend invite ──


@pytest.mark.asyncio
async def test_resend_invite_setup_url_uses_base_url_override(
    app, client, db_session, monkeypatch
):
    """The resend-invite endpoint must honour the same base-URL override."""
    await _setup_smtp(db_session)
    user = await _create_pending_user(db_session, email="pending2@test.com")

    _set_base_url_env(monkeypatch, "https://agora.egnw.org")

    async with _capture_welcome_setup_url(monkeypatch) as captured:
        resp = await client.post(f"/api/users/{user.id}/resend-invite")
        assert resp.status_code == 200, resp.text

    setup_url = captured.get("setup_url")
    assert setup_url, "send_welcome_email_background was never called"
    assert setup_url.startswith("https://agora.egnw.org/setup-account?token="), (
        f"expected custom-domain prefix, got {setup_url!r}"
    )


@pytest.mark.asyncio
async def test_base_url_override_with_trailing_slash_is_normalised(
    app, client, db_session, monkeypatch
):
    """A trailing slash on AGORA_CMS_BASE_URL must not produce '//setup-account'."""
    await _setup_smtp(db_session)
    role_id = await _get_role_id(db_session, "Viewer")

    _set_base_url_env(monkeypatch, "https://agora.egnw.org/")

    async with _capture_welcome_setup_url(monkeypatch) as captured:
        resp = await client.post(
            "/api/users",
            json={
                "email": "slashy@test.com",
                "display_name": "Slashy",
                "role_id": str(role_id),
                "group_ids": [],
            },
        )
        assert resp.status_code == 201, resp.text

    setup_url = captured.get("setup_url")
    assert setup_url, "send_welcome_email_background was never called"
    assert "//setup-account" not in setup_url, (
        f"trailing slash on base_url leaked into the URL: {setup_url!r}"
    )
    assert setup_url.startswith("https://agora.egnw.org/setup-account?token="), (
        f"expected custom-domain prefix, got {setup_url!r}"
    )
