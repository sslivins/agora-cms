"""Guarded-network policy for composed-slide thumbnail snapshots.

`render_composed_to_png` used to abort *all* network so weather /
web-embed widgets only ever rendered their offline fallback in the
grid thumbnail. It now allows live ``http(s)`` requests whose host
passes the SSRF guard (``_host_is_safe``) while still aborting
private / internal hosts and every non-``data:`` / non-http scheme.

These tests exercise:
* `_composed_scheme_action` scheme classification.
* The real route-guard closure inside `render_composed_to_png`, driven
  through a fake Playwright browser, with `_host_is_safe` stubbed so no
  real DNS happens: data: continues, safe host continues, unsafe host
  aborts, file/blob/ws abort, and each host is resolved at most once
  (per-render cache).
"""

import pytest

import worker.composed_render as wcr


class TestComposedSchemeAction:
    @pytest.mark.parametrize(
        "url",
        [
            "data:image/png;base64,AAAA",
            "DATA:text/html,<b>hi</b>",
        ],
    )
    def test_data_allow(self, url):
        assert wcr._composed_scheme_action(url) == "allow"

    @pytest.mark.parametrize(
        "url",
        [
            "https://api.open-meteo.com/v1/forecast",
            "http://example.com/page",
            "HTTPS://Example.com/X",
        ],
    )
    def test_http_check_host(self, url):
        assert wcr._composed_scheme_action(url) == "check_host"

    @pytest.mark.parametrize(
        "url",
        [
            "file:///etc/passwd",
            "blob:https://x/abc",
            "ws://example.com/socket",
            "ftp://example.com/x",
            "javascript:alert(1)",
        ],
    )
    def test_other_abort(self, url):
        assert wcr._composed_scheme_action(url) == "abort"


class _FakeRoute:
    def __init__(self, url):
        self.request = type("Req", (), {"url": url})()
        self.action = None

    async def continue_(self):
        self.action = "continue"

    async def abort(self):
        self.action = "abort"


class _FakePage:
    def __init__(self):
        self._handler = None

    async def route(self, pattern, handler):
        self._handler = handler

    def set_default_timeout(self, _ms):
        pass

    async def set_content(self, _html, **_kw):
        pass

    async def wait_for_load_state(self, _state, **_kw):
        pass

    async def evaluate(self, _js):
        return None

    async def wait_for_timeout(self, _ms):
        pass

    async def screenshot(self, **_kw):
        return b"\x89PNG-fake"


class _FakeContext:
    def __init__(self, page):
        self._page = page

    async def new_page(self):
        return self._page

    async def close(self):
        pass


class _FakeBrowser:
    def __init__(self, page):
        self._page = page

    async def new_context(self, **_kw):
        return _FakeContext(self._page)


@pytest.mark.asyncio
class TestComposedRouteGuard:
    async def _capture_guard(self, monkeypatch, resolved):
        """Run render_composed_to_png with a fake browser; return the
        captured route guard + a record of hosts passed to _host_is_safe.

        ``resolved`` maps host -> bool returned by the stubbed
        _host_is_safe.
        """
        page = _FakePage()
        monkeypatch.setattr(
            wcr, "_get_browser", lambda: _fake_async(_FakeBrowser(page)),
        )

        seen = []

        def _fake_safe(host):
            seen.append(host)
            return resolved.get(host, True)

        monkeypatch.setattr(wcr, "_host_is_safe", _fake_safe)

        out = await wcr.render_composed_to_png(b"<html></html>")
        assert out == b"\x89PNG-fake"
        assert page._handler is not None
        return page._handler, seen

    async def test_data_uri_continues(self, monkeypatch):
        guard, seen = await self._capture_guard(monkeypatch, {})
        r = _FakeRoute("data:image/png;base64,AAAA")
        await guard(r)
        assert r.action == "continue"
        assert seen == []  # no DNS for data:

    async def test_safe_host_continues(self, monkeypatch):
        guard, seen = await self._capture_guard(
            monkeypatch, {"api.open-meteo.com": True},
        )
        r = _FakeRoute("https://api.open-meteo.com/v1/forecast")
        await guard(r)
        assert r.action == "continue"
        assert seen == ["api.open-meteo.com"]

    async def test_unsafe_host_aborts(self, monkeypatch):
        guard, _ = await self._capture_guard(
            monkeypatch, {"169.254.169.254": False},
        )
        r = _FakeRoute("http://169.254.169.254/latest/meta-data")
        await guard(r)
        assert r.action == "abort"

    async def test_file_scheme_aborts(self, monkeypatch):
        guard, seen = await self._capture_guard(monkeypatch, {})
        r = _FakeRoute("file:///etc/passwd")
        await guard(r)
        assert r.action == "abort"
        assert seen == []  # never resolved a host

    async def test_host_resolved_once(self, monkeypatch):
        guard, seen = await self._capture_guard(
            monkeypatch, {"example.com": True},
        )
        for _ in range(3):
            r = _FakeRoute("https://example.com/asset")
            await guard(r)
            assert r.action == "continue"
        assert seen == ["example.com"]  # cached after first resolve


async def _fake_async(value):
    return value
