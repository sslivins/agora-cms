"""Phase 8: MCP server (#250).

Exercises the MCP server integration end-to-end against the real compose
stack:

1. Admin enables MCP via ``POST /api/mcp/toggle`` — CMS auto-provisions a
   service key and writes it to the shared ``mcp-shared`` volume for the
   MCP container to pick up.
2. Operator A creates a self-service ``key_type=mcp`` key via
   ``POST /api/keys/my``; also creates an ``api``-type key for negative
   coverage.
3. Validate ``GET /api/mcp/auth`` behaviour:
     - valid MCP key → 200 + role/permissions payload
     - API-type key  → 403 ("Only MCP keys…")
     - bogus token   → 401
     - missing token → 401
4. Validate the MCP container itself:
     - ``GET /health`` responds 200 (no auth)
     - ``GET /health/api`` succeeds — proves service key round-trips back to
       the CMS correctly (the MCP→CMS trust chain).
5. Disable MCP and verify ``/api/mcp/auth`` starts returning 403; re-enable
   afterwards so later nightly phases aren't affected.

Depends on Phase 7 having created ``OPERATOR_A`` with ``MCP_KEYS_SELF``
(built-in Operator role).
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx
import pytest
from playwright.sync_api import BrowserContext, Page

from tests.nightly.test_06_rbac import OPERATOR_A, VIEWER, _login_page


# MCP container's host-exposed port (docker-compose.yml publishes 8000:8000).
MCP_BASE_URL_DEFAULT = "http://127.0.0.1:8000"


# Shared state for the module — subsequent tests read these.
MCP_STATE: dict[str, Any] = {
    "operator_mcp_key": None,   # raw 'agora_...' string, shown once on creation
    "operator_api_key": None,   # api-type key for negative coverage
    "service_key": None,        # raw service key returned on first toggle-on
}


# ── helpers ───────────────────────────────────────────────────────────────


def _admin_toggle_mcp(admin_page: Page, enabled: bool) -> dict:
    resp = admin_page.request.post(
        "/api/mcp/toggle",
        data={"enabled": enabled},
    )
    assert resp.status == 200, f"toggle -> {resp.status}: {resp.text()[:400]}"
    return resp.json()


def _create_my_key(page: Page, *, name: str, key_type: str) -> dict:
    resp = page.request.post(
        "/api/keys/my",
        data={"name": name, "key_type": key_type},
    )
    assert resp.status == 201, f"create key -> {resp.status}: {resp.text()[:400]}"
    return resp.json()


def _mcp_auth(cms_base_url: str, token: str | None) -> httpx.Response:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.get(f"{cms_base_url.rstrip('/')}/api/mcp/auth", headers=headers, timeout=5.0)


def _mcp_base_url() -> str:
    import os
    return os.environ.get("NIGHTLY_MCP_URL", MCP_BASE_URL_DEFAULT)


# ── tests ─────────────────────────────────────────────────────────────────


def test_01_admin_enables_mcp(authenticated_page: Page) -> None:
    """Admin toggles MCP on — CMS auto-provisions a service key."""
    result = _admin_toggle_mcp(authenticated_page, enabled=True)
    assert result.get("enabled") is True
    # First enable (freshly-wiped DB) auto-provisions a service key and returns
    # it. Save for later assertions; subsequent toggles won't re-generate.
    if "service_key" in result:
        MCP_STATE["service_key"] = result["service_key"]
        assert result["service_key"].startswith("agora_")


def test_02_operator_creates_mcp_key(browser_context: BrowserContext) -> None:
    """Operator A self-provisions an MCP-type key (has ``MCP_KEYS_SELF``)."""
    op_page = _login_page(browser_context, OPERATOR_A["email"], OPERATOR_A["password"])

    mcp_key = _create_my_key(op_page, name="nightly-mcp-key", key_type="mcp")
    assert mcp_key["key"].startswith("agora_"), f"unexpected key prefix: {mcp_key['key'][:12]}"
    assert mcp_key["key_type"] == "mcp"

    MCP_STATE["operator_mcp_key"] = mcp_key["key"]


def test_02b_admin_creates_api_type_key_for_negative_test(authenticated_page: Page) -> None:
    """Admin creates an ``api``-type key used later to prove /api/mcp/auth rejects it.

    Operator role doesn't grant ``API_KEYS_SELF``; admin has all permissions,
    so use the admin context here.
    """
    api_key = _create_my_key(authenticated_page, name="nightly-admin-api-key", key_type="api")
    assert api_key["key"].startswith("agora_")
    assert api_key["key_type"] == "api"
    MCP_STATE["operator_api_key"] = api_key["key"]


def test_03_viewer_cannot_create_mcp_key_without_permission(
    browser_context: BrowserContext,
) -> None:
    """Viewer role lacks ``MCP_KEYS_SELF`` — POST /api/keys/my {mcp} is 403."""
    v_page = _login_page(browser_context, VIEWER["email"], VIEWER["password"])
    resp = v_page.request.post(
        "/api/keys/my",
        data={"name": "viewer-mcp-key", "key_type": "mcp"},
    )
    assert resp.status == 403, f"viewer creating MCP key -> {resp.status}: {resp.text()[:400]}"


def test_04_mcp_auth_accepts_mcp_key(cms_base_url: str) -> None:
    """/api/mcp/auth with Operator A's MCP key returns 200 + Operator permissions."""
    token = MCP_STATE["operator_mcp_key"]
    assert token, "prior test must have populated MCP_STATE['operator_mcp_key']"

    r = _mcp_auth(cms_base_url, token)
    assert r.status_code == 200, f"expected 200, got {r.status_code}: {r.text[:400]}"
    body = r.json()
    assert body["valid"] is True
    assert body["role"] == "Operator"
    assert body["key_type"] == "mcp"
    # Operator role should have at least a few known permissions.
    perms = set(body.get("permissions") or [])
    for expected in ("devices:read", "groups:read", "schedules:read"):
        assert expected in perms, f"Operator perms missing {expected}; got {sorted(perms)}"


def test_05_mcp_auth_rejects_api_type_key(cms_base_url: str) -> None:
    """api-type key must not authenticate on the MCP auth endpoint."""
    token = MCP_STATE["operator_api_key"]
    assert token
    r = _mcp_auth(cms_base_url, token)
    assert r.status_code == 403, f"expected 403, got {r.status_code}: {r.text[:400]}"
    assert "mcp" in r.text.lower()


def test_06_mcp_auth_rejects_invalid_token(cms_base_url: str) -> None:
    r = _mcp_auth(cms_base_url, "agora_deadbeef" + "00" * 20)
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text[:400]}"


def test_07_mcp_auth_requires_bearer_header(cms_base_url: str) -> None:
    r = _mcp_auth(cms_base_url, None)
    assert r.status_code == 401, f"expected 401, got {r.status_code}: {r.text[:400]}"


def test_08_mcp_container_health(cms_base_url: str) -> None:
    """MCP container is up and can reach the CMS with its service key."""
    base = _mcp_base_url()

    # /health is plain — no auth. Proves the MCP Starlette app is listening.
    r = httpx.get(f"{base}/health", timeout=5.0)
    assert r.status_code == 200, f"MCP /health -> {r.status_code}: {r.text[:400]}"

    # /health/api proves the MCP→CMS trust chain works. It uses the service
    # key injected by `POST /api/mcp/toggle` → list_devices against the CMS.
    # The service key propagation happens via either the shared volume or a
    # /reload-key ping from the CMS; be generous with timeout.
    deadline = time.monotonic() + 30.0
    last_body: str = ""
    last_status: int = 0
    while time.monotonic() < deadline:
        r = httpx.get(f"{base}/health/api", timeout=5.0)
        last_status, last_body = r.status_code, r.text
        if r.status_code == 200:
            body = r.json()
            if body.get("status") == "ok":
                return
        time.sleep(1.0)
    pytest.fail(
        f"MCP /health/api never reported status=ok; last status={last_status}, body={last_body[:400]}"
    )


def test_09_mcp_auth_rejected_when_disabled(
    authenticated_page: Page, cms_base_url: str
) -> None:
    """Toggling MCP off causes /api/mcp/auth to 403 even with a valid key.

    Restores MCP to enabled state before returning so later test sessions
    (or re-runs) aren't left in a disabled state.
    """
    token = MCP_STATE["operator_mcp_key"]
    assert token

    _admin_toggle_mcp(authenticated_page, enabled=False)
    try:
        r = _mcp_auth(cms_base_url, token)
        assert r.status_code == 403, (
            f"expected 403 when MCP disabled, got {r.status_code}: {r.text[:400]}"
        )
        assert "disabled" in r.text.lower()
    finally:
        _admin_toggle_mcp(authenticated_page, enabled=True)

    # Sanity: after re-enable, the same key works again.
    r = _mcp_auth(cms_base_url, token)
    assert r.status_code == 200, (
        f"re-enabled MCP: expected 200, got {r.status_code}: {r.text[:400]}"
    )


def test_10_operator_sees_only_own_mcp_key_in_list(
    browser_context: BrowserContext,
) -> None:
    """GET /api/keys/my returns only the operator's own keys."""
    op_page = _login_page(browser_context, OPERATOR_A["email"], OPERATOR_A["password"])
    resp = op_page.request.get("/api/keys/my")
    assert resp.status == 200
    keys = resp.json()
    names = {k["name"] for k in keys}
    assert "nightly-mcp-key" in names
    # Admin's API-type key must NOT be visible to the operator (self-service
    # endpoint returns only the caller's own keys).
    assert "nightly-admin-api-key" not in names
    mcp_keys = [k for k in keys if k["key_type"] == "mcp"]
    assert len(mcp_keys) == 1
