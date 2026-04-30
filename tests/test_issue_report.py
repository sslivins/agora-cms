"""Tests for the "Report an issue" feature."""

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest


# ──────────────────────────────────────────────────────────────────────
# Service-level tests: cms/services/issue_reporter.py
# ──────────────────────────────────────────────────────────────────────


@pytest.fixture
def with_token(monkeypatch):
    """Patch get_settings() inside issue_reporter to return a token-enabled settings object."""
    from cms.services import issue_reporter

    fake_settings = MagicMock()
    fake_settings.github_issues_token = "ghp_test"
    fake_settings.github_issues_repo = "owner/repo"
    fake_settings.github_issues_label = "user-reported"

    monkeypatch.setattr(issue_reporter, "get_settings", lambda: fake_settings)
    return fake_settings


@pytest.fixture
def without_token(monkeypatch):
    from cms.services import issue_reporter

    fake_settings = MagicMock()
    fake_settings.github_issues_token = None
    fake_settings.github_issues_repo = "owner/repo"
    fake_settings.github_issues_label = "user-reported"
    monkeypatch.setattr(issue_reporter, "get_settings", lambda: fake_settings)
    return fake_settings


def _patched_async_client(handler):
    """Build an httpx.AsyncClient that uses a MockTransport via patch context manager."""
    transport = httpx.MockTransport(handler)
    # Use the unmonkey-patched AsyncClient class so this works even when
    # the test has replaced ``issue_reporter.httpx.AsyncClient``.
    return _ORIG_ASYNC_CLIENT(transport=transport, timeout=15)


_ORIG_ASYNC_CLIENT = httpx.AsyncClient


def test_is_enabled_true(with_token):
    from cms.services import issue_reporter
    assert issue_reporter.is_enabled() is True


def test_is_enabled_false(without_token):
    from cms.services import issue_reporter
    assert issue_reporter.is_enabled() is False


@pytest.mark.asyncio
async def test_create_issue_success(with_token, monkeypatch):
    from cms.services import issue_reporter

    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["headers"] = dict(request.headers)
        import json
        captured["body"] = json.loads(request.content.decode())
        return httpx.Response(
            201,
            json={"number": 42, "html_url": "https://github.com/owner/repo/issues/42"},
        )

    # Replace AsyncClient(...) call with one bound to our mock transport
    def fake_client_factory(*args, **kwargs):
        return _patched_async_client(handler)

    monkeypatch.setattr(issue_reporter.httpx, "AsyncClient", fake_client_factory)

    result = await issue_reporter.create_issue(
        "hi", "body text", labels=["user-reported"]
    )
    assert result["number"] == 42
    assert captured["url"] == "https://api.github.com/repos/owner/repo/issues"
    assert captured["body"]["title"] == "hi"
    assert captured["body"]["body"] == "body text"
    assert captured["body"]["labels"] == ["user-reported"]
    assert captured["headers"]["authorization"] == "Bearer ghp_test"


@pytest.mark.asyncio
async def test_create_issue_no_token_raises_503(without_token):
    from cms.services import issue_reporter

    with pytest.raises(issue_reporter.IssueReporterError) as exc_info:
        await issue_reporter.create_issue("t", "b")
    assert exc_info.value.status_code == 503


@pytest.mark.asyncio
async def test_create_issue_unauthorized(with_token, monkeypatch):
    from cms.services import issue_reporter

    def handler(request):
        return httpx.Response(401, json={"message": "Bad credentials"})

    monkeypatch.setattr(
        issue_reporter.httpx, "AsyncClient", lambda *a, **kw: _patched_async_client(handler)
    )

    with pytest.raises(issue_reporter.IssueReporterError) as exc_info:
        await issue_reporter.create_issue("t", "b")
    assert exc_info.value.status_code == 502


@pytest.mark.asyncio
async def test_create_issue_network_error(with_token, monkeypatch):
    from cms.services import issue_reporter

    def handler(request):
        raise httpx.ConnectError("boom")

    monkeypatch.setattr(
        issue_reporter.httpx, "AsyncClient", lambda *a, **kw: _patched_async_client(handler)
    )

    with pytest.raises(issue_reporter.IssueReporterError) as exc_info:
        await issue_reporter.create_issue("t", "b")
    assert exc_info.value.status_code == 502


# ──────────────────────────────────────────────────────────────────────
# Endpoint tests: POST /api/issues/report
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_report_endpoint_disabled_returns_503(client, monkeypatch):
    """When no token is configured, the endpoint refuses with 503."""
    from cms.services import issue_reporter

    monkeypatch.setattr(issue_reporter, "is_enabled", lambda: False)

    resp = await client.post(
        "/api/issues/report",
        json={"title": "t", "description": "d"},
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_report_endpoint_creates_issue(client, monkeypatch):
    from cms.services import issue_reporter

    monkeypatch.setattr(issue_reporter, "is_enabled", lambda: True)

    create_mock = AsyncMock(
        return_value={"number": 7, "html_url": "https://github.com/owner/repo/issues/7"}
    )
    monkeypatch.setattr(issue_reporter, "create_issue", create_mock)

    resp = await client.post(
        "/api/issues/report",
        json={
            "title": "Bug on schedules tab",
            "description": "Stuff broke",
            "page_url": "http://localhost/schedules",
            "user_agent": "Mozilla/5.0 test",
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["number"] == 7
    assert data["html_url"].endswith("/issues/7")

    create_mock.assert_awaited_once()
    args, kwargs = create_mock.await_args
    assert args[0] == "Bug on schedules tab"
    body = args[1]
    # Body should include description and the captured context block
    assert "Stuff broke" in body
    assert "http://localhost/schedules" in body
    assert "Mozilla/5.0 test" in body
    assert "**CMS version:**" in body
    assert kwargs.get("labels") == ["user-reported"]


@pytest.mark.asyncio
async def test_report_endpoint_validates_input(client, monkeypatch):
    from cms.services import issue_reporter

    monkeypatch.setattr(issue_reporter, "is_enabled", lambda: True)

    # Missing description
    resp = await client.post("/api/issues/report", json={"title": "t"})
    assert resp.status_code == 422

    # Empty title
    resp = await client.post("/api/issues/report", json={"title": "", "description": "d"})
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_report_endpoint_requires_auth(unauthed_client):
    resp = await unauthed_client.post(
        "/api/issues/report",
        json={"title": "t", "description": "d"},
    )
    assert resp.status_code in (401, 403, 302)


@pytest.mark.asyncio
async def test_report_endpoint_falls_back_to_request_headers(client, monkeypatch):
    """When client doesn't supply page_url/user_agent, server uses Referer + User-Agent headers."""
    from cms.services import issue_reporter

    monkeypatch.setattr(issue_reporter, "is_enabled", lambda: True)

    create_mock = AsyncMock(
        return_value={"number": 1, "html_url": "https://github.com/owner/repo/issues/1"}
    )
    monkeypatch.setattr(issue_reporter, "create_issue", create_mock)

    resp = await client.post(
        "/api/issues/report",
        json={"title": "t", "description": "d"},
        headers={"Referer": "http://test/devices", "User-Agent": "ServerPickedUA"},
    )
    assert resp.status_code == 200, resp.text
    body = create_mock.await_args.args[1]
    assert "http://test/devices" in body
    assert "ServerPickedUA" in body


# ──────────────────────────────────────────────────────────────────────
# Topbar button visibility (template integration)
# ──────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_topbar_button_visible_when_token_configured(client, monkeypatch):
    """The 'Report an issue' button renders in the topbar when a token is set."""
    from cms import ui as cms_ui

    fake_settings = MagicMock()
    fake_settings.github_issues_token = "ghp_test"
    monkeypatch.setattr(cms_ui, "get_settings", lambda: fake_settings)

    resp = await client.get("/")
    assert resp.status_code == 200
    assert 'id="report-issue-btn"' in resp.text


@pytest.mark.asyncio
async def test_topbar_button_hidden_when_token_missing(client, monkeypatch):
    """The 'Report an issue' button is hidden when no GitHub token is configured."""
    from cms import ui as cms_ui

    fake_settings = MagicMock()
    fake_settings.github_issues_token = None
    monkeypatch.setattr(cms_ui, "get_settings", lambda: fake_settings)

    resp = await client.get("/")
    assert resp.status_code == 200
    assert 'id="report-issue-btn"' not in resp.text
