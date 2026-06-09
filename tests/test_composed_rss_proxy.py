"""Tests for cms.composed.rss_proxy (SSRF-guarded RSS/Atom feed proxy)."""

from __future__ import annotations

import socket

import pytest

from cms.composed import rss_proxy
from cms.composed.rss_proxy import (
    RssProxyError,
    _validate_target,
    clamp_item_count,
    parse_feed,
)

RSS_2_0 = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <title>Example Feed</title>
    <item>
      <title>First headline</title>
      <link>https://example.com/1</link>
      <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Second headline</title>
      <link>https://example.com/2</link>
    </item>
    <item>
      <link>https://example.com/3</link>
    </item>
  </channel>
</rss>
"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?>
<feed xmlns="http://www.w3.org/2005/Atom">
  <title>Atom Example</title>
  <entry>
    <title>Atom one</title>
    <link href="https://example.com/a1" rel="alternate"/>
    <published>2024-01-01T12:00:00Z</published>
  </entry>
  <entry>
    <title>Atom two</title>
    <link href="https://example.com/a2"/>
  </entry>
</feed>
"""


class TestClampItemCount:
    def test_none_returns_default(self):
        assert clamp_item_count(None) == 10

    def test_bad_string_returns_default(self):
        assert clamp_item_count("abc") == 10  # type: ignore[arg-type]

    def test_below_floor_clamped_to_one(self):
        assert clamp_item_count(0) == 1
        assert clamp_item_count(-5) == 1

    def test_above_ceiling_clamped(self):
        assert clamp_item_count(99) == 30

    def test_in_range_passthrough(self):
        assert clamp_item_count(7) == 7


class TestParseFeedRss:
    def test_parses_title_link_pubdate(self):
        items = parse_feed(RSS_2_0, count=10)
        assert items[0] == {
            "title": "First headline",
            "link": "https://example.com/1",
            "pubDate": "Mon, 01 Jan 2024 12:00:00 GMT",
        }

    def test_link_optional(self):
        items = parse_feed(RSS_2_0, count=10)
        assert items[1] == {
            "title": "Second headline",
            "link": "https://example.com/2",
        }

    def test_titleless_item_skipped(self):
        items = parse_feed(RSS_2_0, count=10)
        # Three <item>s but the last has no title -> only 2 returned.
        assert len(items) == 2
        assert all(i["title"] for i in items)

    def test_count_caps_results(self):
        items = parse_feed(RSS_2_0, count=1)
        assert len(items) == 1
        assert items[0]["title"] == "First headline"


class TestParseFeedAtom:
    def test_parses_entry_href_published(self):
        items = parse_feed(ATOM, count=10)
        assert items[0] == {
            "title": "Atom one",
            "link": "https://example.com/a1",
            "pubDate": "2024-01-01T12:00:00Z",
        }

    def test_atom_link_uses_href_attribute(self):
        items = parse_feed(ATOM, count=10)
        assert items[1]["link"] == "https://example.com/a2"


class TestParseFeedErrors:
    def test_bad_xml_raises_502(self):
        with pytest.raises(RssProxyError) as exc:
            parse_feed(b"<not valid xml", count=10)
        assert exc.value.status_code == 502

    def test_empty_feed_returns_empty_list(self):
        body = b'<?xml version="1.0"?><rss version="2.0"><channel></channel></rss>'
        assert parse_feed(body, count=10) == []


class TestParseFeedSortNewest:
    SORTABLE = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Older</title>
      <link>https://example.com/old</link>
      <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Newer</title>
      <link>https://example.com/new</link>
      <pubDate>Wed, 03 Jan 2024 12:00:00 GMT</pubDate>
    </item>
    <item>
      <title>Middle</title>
      <link>https://example.com/mid</link>
      <pubDate>Tue, 02 Jan 2024 12:00:00 GMT</pubDate>
    </item>
  </channel>
</rss>
"""

    def test_default_preserves_document_order(self):
        items = parse_feed(self.SORTABLE, count=10)
        assert [i["title"] for i in items] == ["Older", "Newer", "Middle"]

    def test_sort_newest_orders_by_date(self):
        items = parse_feed(self.SORTABLE, count=10, sort_newest=True)
        assert [i["title"] for i in items] == ["Newer", "Middle", "Older"]

    def test_sort_newest_truncates_after_sorting(self):
        items = parse_feed(self.SORTABLE, count=1, sort_newest=True)
        assert len(items) == 1
        assert items[0]["title"] == "Newer"

    def test_undated_items_sort_last_in_doc_order(self):
        body = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item><title>No date A</title><link>https://example.com/a</link></item>
    <item>
      <title>Dated</title><link>https://example.com/d</link>
      <pubDate>Mon, 01 Jan 2024 12:00:00 GMT</pubDate>
    </item>
    <item><title>No date B</title><link>https://example.com/b</link></item>
  </channel>
</rss>
"""
        items = parse_feed(body, count=10, sort_newest=True)
        assert [i["title"] for i in items] == ["Dated", "No date A", "No date B"]


class TestParseFeedWhitespace:
    def test_internal_newlines_collapsed_in_title(self):
        body = b"""<?xml version="1.0"?>
<rss version="2.0">
  <channel>
    <item>
      <title>Breaking:
      multi   line
      headline</title>
      <link>https://example.com/1</link>
    </item>
  </channel>
</rss>
"""
        items = parse_feed(body, count=10)
        assert items[0]["title"] == "Breaking: multi line headline"


class TestValidateTargetScheme:
    def test_rejects_ftp(self):
        with pytest.raises(RssProxyError) as exc:
            _validate_target("ftp://example.com/feed.xml")
        assert exc.value.status_code == 400

    def test_rejects_no_host(self):
        with pytest.raises(RssProxyError) as exc:
            _validate_target("https:///feed.xml")
        assert exc.value.status_code == 400


class TestValidateTargetSsrfLiterals:
    @pytest.mark.parametrize(
        "url",
        [
            "http://127.0.0.1/feed.xml",
            "http://169.254.169.254/latest/meta-data/",
            "http://10.0.0.1/feed.xml",
            "http://192.168.1.5/feed.xml",
            "http://[::1]/feed.xml",
            "http://0.0.0.0/feed.xml",
        ],
    )
    def test_blocks_private_and_special_ip_literals(self, url):
        with pytest.raises(RssProxyError) as exc:
            _validate_target(url)
        assert exc.value.status_code == 400

    def test_allows_public_ip_literal(self):
        # 1.1.1.1 (Cloudflare) is a public address — must not raise.
        _validate_target("https://1.1.1.1/feed.xml")


class TestValidateTargetSsrfResolution:
    def test_blocks_host_resolving_to_private(self, monkeypatch):
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("10.1.2.3", 0))]

        monkeypatch.setattr(rss_proxy.socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(RssProxyError) as exc:
            _validate_target("https://evil.example.com/feed.xml")
        assert exc.value.status_code == 400

    def test_allows_host_resolving_to_public(self, monkeypatch):
        def fake_getaddrinfo(host, port, *args, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 0))]

        monkeypatch.setattr(rss_proxy.socket, "getaddrinfo", fake_getaddrinfo)
        _validate_target("https://example.com/feed.xml")

    def test_unresolvable_host_400(self, monkeypatch):
        def fake_getaddrinfo(host, port, *args, **kwargs):
            raise socket.gaierror("name resolution failed")

        monkeypatch.setattr(rss_proxy.socket, "getaddrinfo", fake_getaddrinfo)
        with pytest.raises(RssProxyError) as exc:
            _validate_target("https://nope.invalid/feed.xml")
        assert exc.value.status_code == 400


@pytest.mark.asyncio
class TestRssProxyRoute:
    async def test_returns_items_with_cors(self, unauthed_client, monkeypatch):
        async def fake_fetch(url, *, count, sort_newest=True):
            return [{"title": "Hello", "link": "https://example.com/1"}]

        monkeypatch.setattr(rss_proxy, "fetch_feed_items", fake_fetch)
        resp = await unauthed_client.get(
            "/composed/rss", params={"url": "https://example.com/feed.xml"}
        )
        assert resp.status_code == 200
        assert resp.headers["access-control-allow-origin"] == "*"
        assert resp.headers["cache-control"] == "no-store"
        assert resp.json() == {"items": [{"title": "Hello", "link": "https://example.com/1"}]}

    async def test_error_returns_status_and_message(self, unauthed_client, monkeypatch):
        async def fake_fetch(url, *, count, sort_newest=True):
            raise RssProxyError(400, "Feed host is not allowed")

        monkeypatch.setattr(rss_proxy, "fetch_feed_items", fake_fetch)
        resp = await unauthed_client.get(
            "/composed/rss", params={"url": "http://127.0.0.1/feed.xml"}
        )
        assert resp.status_code == 400
        assert resp.headers["access-control-allow-origin"] == "*"
        assert resp.json() == {"error": "Feed host is not allowed"}

    async def test_count_is_clamped_before_fetch(self, unauthed_client, monkeypatch):
        seen = {}

        async def fake_fetch(url, *, count, sort_newest=True):
            seen["count"] = count
            return []

        monkeypatch.setattr(rss_proxy, "fetch_feed_items", fake_fetch)
        resp = await unauthed_client.get(
            "/composed/rss",
            params={"url": "https://example.com/feed.xml", "count": 999},
        )
        assert resp.status_code == 200
        assert seen["count"] == 30

    async def test_newest_defaults_true_and_passes_through(
        self, unauthed_client, monkeypatch
    ):
        seen = {}

        async def fake_fetch(url, *, count, sort_newest=True):
            seen["sort_newest"] = sort_newest
            return []

        monkeypatch.setattr(rss_proxy, "fetch_feed_items", fake_fetch)
        resp = await unauthed_client.get(
            "/composed/rss",
            params={"url": "https://example.com/feed.xml"},
        )
        assert resp.status_code == 200
        assert seen["sort_newest"] is True

    async def test_newest_zero_disables_sort(self, unauthed_client, monkeypatch):
        seen = {}

        async def fake_fetch(url, *, count, sort_newest=True):
            seen["sort_newest"] = sort_newest
            return []

        monkeypatch.setattr(rss_proxy, "fetch_feed_items", fake_fetch)
        resp = await unauthed_client.get(
            "/composed/rss",
            params={"url": "https://example.com/feed.xml", "newest": 0},
        )
        assert resp.status_code == 200
        assert seen["sort_newest"] is False

    async def test_route_requires_no_auth(self, unauthed_client, monkeypatch):
        async def fake_fetch(url, *, count, sort_newest=True):
            return []

        monkeypatch.setattr(rss_proxy, "fetch_feed_items", fake_fetch)
        resp = await unauthed_client.get(
            "/composed/rss", params={"url": "https://example.com/feed.xml"}
        )
        # Unauthenticated client gets a real answer, not a 401/redirect.
        assert resp.status_code == 200
