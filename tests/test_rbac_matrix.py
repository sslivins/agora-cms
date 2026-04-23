"""API-level RBAC matrix: every protected endpoint × every role.

This is a coarse-grained "can the role reach this endpoint at all?" test.
Fine-grained scoping (group membership, cross-group reads, ownership, etc.)
is covered by ``test_rbac.py``; the goal here is to catch regressions
where an endpoint either loses its ``require_permission`` guard (privilege
escalation) or grows a stricter guard than intended (broken for lower
roles).

For each endpoint, the matrix declares the roles that should pass
auth (get a 2xx/4xx response that isn't 401/403) and the roles that
should be rejected with 403. Fake UUIDs / empty bodies are fine:
we only care whether the permission gate fires.

Data-driven on purpose — adding a new endpoint should be one line.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Iterable

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from tests.test_rbac import _create_user, _login_as, _seed_smtp


# ── Matrix ──

ADMIN = "Admin"
OPERATOR = "Operator"
VIEWER = "Viewer"
ALL_ROLES = (ADMIN, OPERATOR, VIEWER)


@dataclass(frozen=True)
class EP:
    method: str
    path: str
    allowed: tuple[str, ...]
    # Roles that should reach the endpoint (not 401/403). Non-allowed roles
    # get 403. If ``json_body`` is set, it's sent with the request.
    json_body: dict | None = None

    @property
    def denied(self) -> tuple[str, ...]:
        return tuple(r for r in ALL_ROLES if r not in self.allowed)


_FAKE_ID = "00000000-0000-0000-0000-000000000000"


ENDPOINTS: list[EP] = [
    # ── Devices ──
    EP("GET", "/api/devices", (ADMIN, OPERATOR, VIEWER)),
    EP("GET", f"/api/devices/{_FAKE_ID}", (ADMIN, OPERATOR, VIEWER)),
    EP("POST", "/api/logs/requests", (ADMIN, OPERATOR, VIEWER),
       json_body={"device_id": _FAKE_ID}),
    EP("PATCH", f"/api/devices/{_FAKE_ID}", (ADMIN, OPERATOR), json_body={}),
    EP("POST", "/api/devices/check-updates", (ADMIN,)),
    EP("POST", f"/api/devices/{_FAKE_ID}/password", (ADMIN,), json_body={}),
    EP("POST", f"/api/devices/{_FAKE_ID}/reboot", (ADMIN,)),
    EP("POST", f"/api/devices/{_FAKE_ID}/upgrade", (ADMIN,)),
    EP("POST", f"/api/devices/{_FAKE_ID}/ssh", (ADMIN,), json_body={}),
    EP("POST", f"/api/devices/{_FAKE_ID}/factory-reset", (ADMIN,)),
    EP("POST", f"/api/devices/{_FAKE_ID}/local-api", (ADMIN,), json_body={}),
    EP("POST", f"/api/devices/{_FAKE_ID}/adopt", (ADMIN,)),
    EP("DELETE", f"/api/devices/{_FAKE_ID}", (ADMIN,)),

    # ── Device groups ──
    EP("GET", "/api/devices/groups/", (ADMIN, OPERATOR, VIEWER)),
    EP("POST", "/api/devices/groups/", (ADMIN, OPERATOR), json_body={"name": "x"}),
    EP("PATCH", f"/api/devices/groups/{_FAKE_ID}", (ADMIN, OPERATOR), json_body={}),
    EP("DELETE", f"/api/devices/groups/{_FAKE_ID}", (ADMIN, OPERATOR)),

    # ── Assets ──
    EP("GET", "/api/assets", (ADMIN, OPERATOR, VIEWER)),
    EP("GET", "/api/assets/status", (ADMIN, OPERATOR, VIEWER)),
    EP("GET", f"/api/assets/{_FAKE_ID}", (ADMIN, OPERATOR, VIEWER)),
    EP("GET", f"/api/assets/{_FAKE_ID}/preview", (ADMIN, OPERATOR, VIEWER)),
    EP("POST", "/api/assets/webpage", (ADMIN, OPERATOR), json_body={"url": "https://example.com"}),
    EP("POST", "/api/assets/stream", (ADMIN, OPERATOR), json_body={"url": "rtsp://x"}),
    EP("PATCH", f"/api/assets/{_FAKE_ID}", (ADMIN, OPERATOR), json_body={}),
    EP("POST", f"/api/assets/{_FAKE_ID}/recapture", (ADMIN, OPERATOR)),
    EP("DELETE", f"/api/assets/{_FAKE_ID}", (ADMIN, OPERATOR)),
    EP("POST", f"/api/assets/{_FAKE_ID}/share", (ADMIN, OPERATOR), json_body={"group_id": _FAKE_ID}),
    EP("DELETE", f"/api/assets/{_FAKE_ID}/share", (ADMIN, OPERATOR)),
    EP("POST", f"/api/assets/{_FAKE_ID}/global", (ADMIN, OPERATOR)),

    # ── Schedules ──
    EP("GET", "/api/schedules", (ADMIN, OPERATOR, VIEWER)),
    EP("GET", f"/api/schedules/{_FAKE_ID}", (ADMIN, OPERATOR, VIEWER)),
    EP("POST", "/api/schedules", (ADMIN, OPERATOR), json_body={}),
    EP("PATCH", f"/api/schedules/{_FAKE_ID}", (ADMIN, OPERATOR), json_body={}),
    EP("DELETE", f"/api/schedules/{_FAKE_ID}", (ADMIN, OPERATOR)),
    EP("POST", f"/api/schedules/{_FAKE_ID}/end-now", (ADMIN, OPERATOR)),

    # ── Profiles ──
    EP("GET", "/api/profiles", (ADMIN, OPERATOR, VIEWER)),
    EP("GET", "/api/profiles/status", (ADMIN, OPERATOR, VIEWER)),
    EP("POST", "/api/profiles", (ADMIN,), json_body={}),
    EP("PUT", f"/api/profiles/{_FAKE_ID}", (ADMIN,), json_body={}),
    EP("DELETE", f"/api/profiles/{_FAKE_ID}", (ADMIN,)),
    EP("POST", f"/api/profiles/{_FAKE_ID}/copy", (ADMIN,)),
    EP("POST", f"/api/profiles/{_FAKE_ID}/reset", (ADMIN,)),

    # ── Users ──
    EP("GET", "/api/users", (ADMIN,)),
    EP("GET", f"/api/users/{_FAKE_ID}", (ADMIN,)),
    EP("POST", "/api/users", (ADMIN,), json_body={}),
    EP("PATCH", f"/api/users/{_FAKE_ID}", (ADMIN,), json_body={}),
    EP("DELETE", f"/api/users/{_FAKE_ID}", (ADMIN,)),
    EP("POST", f"/api/users/{_FAKE_ID}/resend-invite", (ADMIN,)),

    # ── Roles ──
    EP("GET", "/api/roles", (ADMIN,)),
    EP("GET", f"/api/roles/{_FAKE_ID}", (ADMIN,)),
    EP("GET", "/api/roles/permissions/catalogue", (ADMIN,)),
    EP("POST", "/api/roles", (ADMIN,), json_body={}),
    EP("PATCH", f"/api/roles/{_FAKE_ID}", (ADMIN,), json_body={}),
    EP("DELETE", f"/api/roles/{_FAKE_ID}", (ADMIN,)),

    # ── Audit ──
    EP("GET", "/api/audit-log", (ADMIN,)),
    EP("GET", "/api/audit-log/count", (ADMIN,)),

    # ── Logs ──
    EP("GET", "/api/cms/logs", (ADMIN, OPERATOR, VIEWER)),

    # ── Admin-managed API keys ──
    EP("GET", "/api/keys", (ADMIN,)),
    EP("POST", "/api/keys", (ADMIN,), json_body={}),
    EP("POST", f"/api/keys/{_FAKE_ID}/regenerate", (ADMIN,)),
    EP("DELETE", f"/api/keys/{_FAKE_ID}", (ADMIN,)),
]


# Endpoints every authenticated user should be able to reach regardless of role.
AUTH_ONLY_ENDPOINTS: list[EP] = [
    EP("GET", "/api/users/me", ALL_ROLES),
    EP("POST", "/api/users/me/password", ALL_ROLES, json_body={}),
    EP("GET", "/api/keys/my", ALL_ROLES),
]


# ── Helpers ──


def _ep_id(ep: EP) -> str:
    """Readable id for parametrize reports."""
    return f"{ep.method} {ep.path}"


def _role_clients_param():
    """Yield (role_name, endpoint) for every (role × endpoint) combo."""
    for ep in ENDPOINTS:
        for role in ALL_ROLES:
            yield role, ep


async def _exec(ac: AsyncClient, ep: EP):
    kwargs: dict = {}
    if ep.json_body is not None:
        kwargs["json"] = ep.json_body
    return await ac.request(ep.method, ep.path, **kwargs)


def _is_permission_gate_denial(resp) -> bool:
    """True iff the 403 came from ``require_permission`` / role check.

    Resource-level 403s (IDOR / scoping guards) fire *after* the permission
    gate passes, so they indicate the gate let the caller through.
    """
    if resp.status_code != 403:
        return False
    try:
        detail = resp.json().get("detail", "")
    except Exception:
        return False
    return (
        isinstance(detail, str)
        and (detail.startswith("Missing permission:") or detail == "No role assigned")
    )


# ── Fixtures ──


@pytest.fixture
def all_endpoints() -> list[EP]:
    return ENDPOINTS + AUTH_ONLY_ENDPOINTS


# ── Matrix test ──


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "role,ep",
    list(_role_clients_param()),
    ids=[f"{role}::{_ep_id(ep)}" for role, ep in _role_clients_param()],
)
async def test_rbac_matrix(app, db_session: AsyncSession, role: str, ep: EP):
    """Each role should only reach the endpoints it owns.

    Allowed = response is not 401/403 (auth/permission gate passed;
    anything downstream like 200, 404, 422 is fine).
    Denied  = response is exactly 403.
    """
    await _seed_smtp(db_session)
    email = f"{role.lower()}-matrix@test.com"
    await _create_user(db_session, email=email, role_name=role)
    ac = await _login_as(app, email)
    try:
        resp = await _exec(ac, ep)
        if role in ep.allowed:
            # Auth + permission gate must pass. 401 or a permission-gate 403
            # are regressions. Resource-level 403s (e.g. group scoping on a
            # fake UUID) are fine — they fire *after* the gate.
            assert resp.status_code != 401 and not _is_permission_gate_denial(resp), (
                f"{role} should be allowed on {_ep_id(ep)} but the permission "
                f"gate rejected with {resp.status_code}: {resp.text[:200]}"
            )
        else:
            assert _is_permission_gate_denial(resp), (
                f"{role} should be denied by the permission gate on "
                f"{_ep_id(ep)} but got {resp.status_code}: {resp.text[:200]}"
            )
    finally:
        await ac.aclose()


# ── Auth-required endpoints (all authenticated roles pass) ──


@pytest.mark.asyncio
@pytest.mark.parametrize("ep", AUTH_ONLY_ENDPOINTS, ids=_ep_id)
@pytest.mark.parametrize("role", ALL_ROLES)
async def test_auth_only_endpoints(app, db_session: AsyncSession, role: str, ep: EP):
    """Endpoints gated only by require_auth should work for every role."""
    await _seed_smtp(db_session)
    email = f"{role.lower()}-authonly@test.com"
    await _create_user(db_session, email=email, role_name=role)
    ac = await _login_as(app, email)
    try:
        resp = await _exec(ac, ep)
        assert resp.status_code != 401 and not _is_permission_gate_denial(resp), (
            f"{role} should reach {_ep_id(ep)} but got {resp.status_code}: {resp.text[:200]}"
        )
    finally:
        await ac.aclose()


# ── Unauthenticated should 401 everywhere ──


@pytest.mark.asyncio
@pytest.mark.parametrize("ep", ENDPOINTS + AUTH_ONLY_ENDPOINTS, ids=_ep_id)
async def test_unauthenticated_rejected(app, ep: EP):
    """Every protected endpoint must reject unauthenticated requests."""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        resp = await _exec(ac, ep)
        assert resp.status_code in (401, 403), (
            f"Unauthenticated request to {_ep_id(ep)} should be rejected "
            f"but got {resp.status_code}: {resp.text[:200]}"
        )
