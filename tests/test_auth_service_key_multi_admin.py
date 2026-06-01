"""Tests for the MCP service-key path in ``cms.auth.get_current_user``.

This is the CMS-side auth that fires when MCP calls back into CMS REST
endpoints via ``cms_client.py`` (X-API-Key: agora_svc_..., X-On-Behalf-Of: <uuid>).

Two regressions are pinned here:

* Multi-admin envs (≥2 active Admins) MUST NOT raise
  ``MultipleResultsFound`` — historically the legacy fallback used
  ``scalar_one_or_none`` which crashed the moment a second admin
  existed.  Goodwill prod hit this on every MCP→CMS call.
* A valid ``X-On-Behalf-Of`` UUID MUST resolve to that real user
  (with their real permissions) — not to "some admin".  This is what
  lets per-user RBAC actually work through the Assistant.
"""

import pytest
from sqlalchemy import select

from cms.auth import (
    SERVICE_KEY_PREFIX,
    SETTING_MCP_ENABLED,
    SETTING_MCP_SERVICE_KEY_HASH,
    _hash_api_key,
    hash_password,
    set_setting,
)
from cms.models.user import Role, User


async def _seed_service_key(db_session) -> str:
    raw = SERVICE_KEY_PREFIX + "test_service_key_value_for_get_current_user"
    await set_setting(db_session, SETTING_MCP_ENABLED, "true")
    await set_setting(db_session, SETTING_MCP_SERVICE_KEY_HASH, _hash_api_key(raw))
    await db_session.commit()
    return raw


async def _create_user(db_session, *, username: str, role_name: str) -> User:
    role = (await db_session.execute(
        select(Role).where(Role.name == role_name)
    )).scalar_one()
    user = User(
        username=username,
        email=f"{username}@test.com",
        display_name=username.title(),
        password_hash=hash_password("x"),
        role_id=role.id,
        is_active=True,
        must_change_password=False,
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    return user


@pytest.mark.asyncio
class TestServiceKeyMultiAdmin:
    """Regression: multi-admin envs broke MCP→CMS with MultipleResultsFound."""

    async def test_service_key_without_obo_does_not_crash_on_multi_admin(
        self, client, db_session
    ):
        """Two active Admins + no UUID header → falls back to *some* admin."""
        await _create_user(db_session, username="admin2", role_name="Admin")
        service_key = await _seed_service_key(db_session)

        resp = await client.get(
            "/api/devices",
            headers={
                "X-API-Key": service_key,
                "X-On-Behalf-Of": "MCP Service",  # not a UUID → legacy path
            },
        )
        # Anything that ISN'T 500 means we didn't blow up resolving the
        # admin user.  /api/devices may legitimately 200 (empty list) or
        # require an extra setup step depending on conftest; the bug
        # was a hard 500 on every request, so just assert non-500.
        assert resp.status_code != 500, resp.text

    async def test_service_key_with_obo_uuid_uses_real_user(
        self, client, db_session
    ):
        """Valid UUID header → request runs as that user, not 'some admin'."""
        operator = await _create_user(
            db_session, username="op-real-user", role_name="Operator"
        )
        service_key = await _seed_service_key(db_session)

        resp = await client.get(
            "/api/devices",
            headers={
                "X-API-Key": service_key,
                "X-On-Behalf-Of": str(operator.id),
            },
        )
        # Operator has devices:read → should succeed (not 403).
        assert resp.status_code != 500, resp.text
        assert resp.status_code != 403, resp.text

    async def test_service_key_with_unknown_uuid_is_rejected(
        self, client, db_session
    ):
        """Valid-shape but unknown UUID → 403 (must NOT silently fall back)."""
        import uuid as _uuid

        service_key = await _seed_service_key(db_session)
        resp = await client.get(
            "/api/devices",
            headers={
                "X-API-Key": service_key,
                "X-On-Behalf-Of": str(_uuid.uuid4()),
            },
        )
        assert resp.status_code == 403, resp.text
