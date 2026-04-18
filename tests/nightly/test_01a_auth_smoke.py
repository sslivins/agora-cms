"""Phase 1a: auth-boundary smoke tests.

Runs right after OOBE and before any feature-layer phase. Catches
catastrophic auth regressions (missing 401s, missing built-in roles, /me
endpoint broken, etc.) with sub-second feedback so later phases don't
produce misleading "can the operator create a schedule?" failures when
the real problem is that auth is dead.

Intentionally minimal and layered:
- No dependency on fixtures from later RBAC/MCP phases.
- HTTP only, no UI automation — `authenticated_page` gives us a cookie
  from the real post-OOBE login flow; we reuse its `request` context
  for authed calls, and plain `httpx` for unauthed ones.
- Read-only. No side effects on the shared stack state.

Named ``test_01a_*`` so it sorts after ``test_01_oobe.py`` (which creates
the admin) and before ``test_02_assets.py`` (the first feature phase).
"""

from __future__ import annotations

import httpx
import pytest
from playwright.sync_api import Page


EXPECTED_BUILTIN_ROLES = {"Admin", "Operator", "Viewer"}


# ── unauthenticated surface ───────────────────────────────────────────────


@pytest.mark.parametrize(
    "path",
    [
        "/api/users/me",
        "/api/users",
        "/api/roles",
        "/api/devices",
    ],
)
def test_unauthenticated_api_is_rejected(cms_base_url: str, path: str) -> None:
    """Core API endpoints must refuse un-cookied callers.

    A regression here means a protected endpoint leaked to anonymous
    callers — worst-case security bug.
    """
    r = httpx.get(f"{cms_base_url}{path}", timeout=5.0, follow_redirects=False)
    assert r.status_code == 401, (
        f"GET {path} without auth returned {r.status_code}, expected 401. "
        f"body={r.text[:200]!r}"
    )


def test_login_page_does_not_require_auth(cms_base_url: str) -> None:
    """The login page itself must always be reachable — otherwise nobody
    can recover from a locked-out state."""
    r = httpx.get(f"{cms_base_url}/login", timeout=5.0)
    assert r.status_code == 200
    assert "html" in r.headers.get("content-type", "").lower()


def test_rejects_bogus_bearer_token(cms_base_url: str) -> None:
    """A random Authorization header must not bypass session auth."""
    r = httpx.get(
        f"{cms_base_url}/api/users/me",
        headers={"Authorization": "Bearer not-a-real-key"},
        timeout=5.0,
        follow_redirects=False,
    )
    assert r.status_code == 401, f"bogus bearer got {r.status_code}"


# ── authenticated surface (admin via the post-OOBE login flow) ────────────


def test_me_identifies_admin(authenticated_page: Page) -> None:
    """/api/users/me must return the current session's user.

    If this fails after OOBE completed, the session cookie isn't being
    honored — every later phase that uses ``authenticated_page`` would
    also fail in unhelpful ways.
    """
    resp = authenticated_page.request.get("/api/users/me")
    assert resp.status == 200, f"/me -> {resp.status}: {resp.text()[:200]}"
    me = resp.json()

    assert me["role"]["name"] == "Admin", f"expected Admin, got {me['role']['name']!r}"
    assert me["role"]["is_builtin"] is True
    assert me["email"], "admin /me should include an email"
    # Admin sees all groups regardless of membership — group_ids may be
    # empty, which is fine. We just assert the field exists and is a list.
    assert isinstance(me.get("group_ids", []), list)
    assert isinstance(me.get("permissions", []), list)
    assert me["permissions"], "admin role should carry a non-empty permission list"


def test_builtin_roles_are_seeded(authenticated_page: Page) -> None:
    """All three built-in roles must exist with ``is_builtin=True``.

    RBAC phase (Phase 7) looks them up by name when creating Operator/Viewer
    users. A missing role here means Phase 7 will fail with a confusing
    KeyError.
    """
    resp = authenticated_page.request.get("/api/roles")
    assert resp.status == 200, f"/api/roles -> {resp.status}: {resp.text()[:200]}"
    roles = resp.json()

    by_name = {r["name"]: r for r in roles}
    missing = EXPECTED_BUILTIN_ROLES - by_name.keys()
    assert not missing, (
        f"missing built-in role(s): {missing}. Got: {sorted(by_name)}"
    )
    for name in EXPECTED_BUILTIN_ROLES:
        assert by_name[name]["is_builtin"] is True, (
            f"role {name!r} exists but is_builtin=False — "
            "possibly clobbered by a seed regression"
        )


def test_permission_catalogue_non_empty(authenticated_page: Page) -> None:
    """The permission catalogue powers the admin UI's role editor; if it
    regresses to empty, the RBAC phase's "grant X permission" calls will
    silently no-op."""
    resp = authenticated_page.request.get("/api/roles/permissions/catalogue")
    assert resp.status == 200, (
        f"/api/roles/permissions/catalogue -> {resp.status}: {resp.text()[:200]}"
    )
    catalogue = resp.json()
    # Shape isn't worth pinning tightly — dict or list both fine — but
    # an empty response is definitely wrong.
    assert catalogue, "permission catalogue is empty"


def test_users_listing_contains_admin(authenticated_page: Page) -> None:
    """Sanity: the admin that Phase 1 OOBE created is visible to itself
    via the listing endpoint (which Phase 7 relies on for uniqueness
    checks when creating new users)."""
    resp = authenticated_page.request.get("/api/users")
    assert resp.status == 200, f"/api/users -> {resp.status}: {resp.text()[:200]}"
    users = resp.json()
    assert isinstance(users, list) and users, "expected at least the admin user"
    admins = [u for u in users if (u.get("role") or {}).get("name") == "Admin"]
    assert admins, f"no Admin-role user in {[u.get('email') for u in users]}"
