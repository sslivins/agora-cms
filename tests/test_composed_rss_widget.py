"""Tests for cms.composed.widgets.rss.RssWidget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import cms.composed.widgets  # noqa: F401
from cms.composed.registry import BundleContext, get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.rss import (
    RssWidget,
    RssWidgetConfig,
    _DEFAULT_FEED_URL,
    _proxy_url,
)


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


class TestRssWidgetConfig:
    def test_defaults(self):
        c = RssWidgetConfig()
        assert c.feed_url == _DEFAULT_FEED_URL
        assert c.heading == ""
        assert c.item_count == 5
        assert c.sort_newest is True
        assert c.show_dates is False
        assert c.color == "#ffffff"
        assert c.font_family == "sans"
        assert c.font_size_px == 32
        assert c.refresh_seconds == 900

    def test_default_config_matches_schema(self):
        # A freshly-placed widget must immediately be a valid config.
        w = RssWidget()
        RssWidgetConfig(**w.default_config())

    def test_font_allowlist(self):
        for ok in ("sans", "serif", "mono"):
            RssWidgetConfig(font_family=ok)
        with pytest.raises(ValidationError):
            RssWidgetConfig(font_family="display")

    def test_feed_url_must_be_http(self):
        RssWidgetConfig(feed_url="http://example.com/feed.xml")
        RssWidgetConfig(feed_url="https://example.com/feed.xml")
        with pytest.raises(ValidationError):
            RssWidgetConfig(feed_url="ftp://example.com/feed.xml")
        with pytest.raises(ValidationError):
            RssWidgetConfig(feed_url="javascript:alert(1)")
        with pytest.raises(ValidationError):
            RssWidgetConfig(feed_url="not-a-url")

    def test_feed_url_stripped(self):
        c = RssWidgetConfig(feed_url="  https://example.com/feed.xml  ")
        assert c.feed_url == "https://example.com/feed.xml"

    def test_feed_url_max_length(self):
        with pytest.raises(ValidationError):
            RssWidgetConfig(feed_url="https://e.com/" + "a" * 2048)

    def test_item_count_bounds(self):
        RssWidgetConfig(item_count=1)
        RssWidgetConfig(item_count=30)
        with pytest.raises(ValidationError):
            RssWidgetConfig(item_count=0)
        with pytest.raises(ValidationError):
            RssWidgetConfig(item_count=31)

    def test_font_size_bounds(self):
        RssWidgetConfig(font_size_px=8)
        RssWidgetConfig(font_size_px=256)
        with pytest.raises(ValidationError):
            RssWidgetConfig(font_size_px=7)
        with pytest.raises(ValidationError):
            RssWidgetConfig(font_size_px=257)

    def test_refresh_floor(self):
        RssWidgetConfig(refresh_seconds=300)
        with pytest.raises(ValidationError):
            RssWidgetConfig(refresh_seconds=299)
        with pytest.raises(ValidationError):
            RssWidgetConfig(refresh_seconds=86401)

    def test_heading_max_length(self):
        RssWidgetConfig(heading="x" * 80)
        with pytest.raises(ValidationError):
            RssWidgetConfig(heading="x" * 81)

    def test_color_must_be_hex_6(self):
        with pytest.raises(ValidationError):
            RssWidgetConfig(color="white")

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            RssWidgetConfig(api_key="secret")  # type: ignore[call-arg]


class TestRssWidgetRegistry:
    def test_registered(self):
        w = get_registry().get("rss")
        assert isinstance(w, RssWidget)
        assert w.slug == "rss"
        assert w.display_name == "RSS Headlines"

    def test_no_declared_assets(self):
        w = RssWidget()
        assert w.declared_asset_ids(RssWidgetConfig()) == []

    def test_validate_semantic_passes(self):
        w = RssWidget()
        assert w.validate_semantic(RssWidgetConfig()) == []


class TestProxyUrl:
    def test_absolute_when_base_set(self):
        url = _proxy_url("https://cms.example.org", "https://feed.test/x.xml", 5)
        assert url.startswith("https://cms.example.org/composed/rss?url=")
        # feed url must be percent-encoded (no raw scheme separators).
        assert "https%3A%2F%2Ffeed.test%2Fx.xml" in url
        assert "&count=5" in url
        assert url.endswith("&newest=1")

    def test_newest_flag_reflected(self):
        on = _proxy_url("https://cms.example.org", "https://feed.test/x.xml", 5)
        off = _proxy_url(
            "https://cms.example.org", "https://feed.test/x.xml", 5, newest=False
        )
        assert on.endswith("&newest=1")
        assert off.endswith("&newest=0")

    def test_relative_when_base_none(self):
        url = _proxy_url(None, "https://feed.test/x.xml", 3)
        assert url.startswith("/composed/rss?url=")

    def test_trailing_slash_stripped(self):
        url = _proxy_url("https://cms.example.org/", "https://feed.test/x.xml", 5)
        assert url.startswith("https://cms.example.org/composed/rss?")
        assert "//composed" not in url.split("?")[0]


class TestRssWidgetRender:
    def test_html_and_css_are_instance_scoped(self):
        w = RssWidget()
        cfg = RssWidgetConfig()
        r1 = w.render_html(cfg, _cell(), "inst-A")
        r2 = w.render_html(cfg, _cell(), "inst-B")
        assert "cw-rss-inst-A" in r1.html
        assert "cw-rss-inst-A" in r1.css
        assert "cw-rss-inst-B" in r2.html
        assert "inst-B" not in r1.html
        assert "inst-A" not in r2.html

    def test_init_js_has_fetch_cache_and_abort(self):
        w = RssWidget()
        r = w.render_html(RssWidgetConfig(), _cell(), "abc")
        assert r.init_js is not None
        js = r.init_js
        assert "fetch(" in js
        assert "localStorage" in js
        assert "AbortController" in js
        # Untrusted feed titles must be painted via textContent, never innerHTML.
        assert "textContent" in js
        assert "innerHTML" not in js

    def test_init_js_uses_absolute_proxy_url_with_ctx(self):
        w = RssWidget()
        ctx = BundleContext(cms_base_url="https://cms.example.org")
        cfg = RssWidgetConfig(feed_url="https://feed.test/x.xml", item_count=7)
        r = w.render_html(cfg, _cell(), "abc", ctx)
        assert r.init_js is not None
        assert "https://cms.example.org/composed/rss?url=" in r.init_js
        # The proxy URL is embedded via _js_str, which HTML-escapes "&" to
        # "\u0026" so a stray "</script>"-style break can't terminate the
        # script block. The count param is therefore present in escaped form.
        assert "count=7" in r.init_js
        assert "\\u0026count=7" in r.init_js

    def test_init_js_uses_relative_proxy_url_without_ctx(self):
        w = RssWidget()
        r = w.render_html(RssWidgetConfig(), _cell(), "abc")
        assert r.init_js is not None
        # No ctx -> relative same-origin proxy URL (preview/thumbnail).
        assert '"/composed/rss?url=' in r.init_js
        assert "https://cms" not in r.init_js

    def test_heading_html_escaped_not_in_js(self):
        w = RssWidget()
        cfg = RssWidgetConfig(heading="<b>News</b> & Co")
        r = w.render_html(cfg, _cell(), "abc")
        assert "&lt;b&gt;News&lt;/b&gt; &amp; Co" in r.html
        assert "<b>News</b>" not in r.html
        # Heading is never passed into JS.
        assert r.init_js is not None
        assert "News" not in r.init_js

    def test_no_heading_markup_when_blank(self):
        w = RssWidget()
        r = w.render_html(RssWidgetConfig(heading=""), _cell(), "abc")
        assert "cw-rss-abc-heading" not in r.html

    def test_cache_key_is_instance_scoped(self):
        w = RssWidget()
        r = w.render_html(RssWidgetConfig(), _cell(), "abc")
        assert r.init_js is not None
        assert "cw-rss-abc" in r.init_js

    def test_show_dates_flag_baked_in(self):
        w = RssWidget()
        r_on = w.render_html(RssWidgetConfig(show_dates=True), _cell(), "abc")
        r_off = w.render_html(RssWidgetConfig(show_dates=False), _cell(), "abc")
        assert r_on.init_js is not None and r_off.init_js is not None
        assert "var SHOW_DATES = true" in r_on.init_js
        assert "var SHOW_DATES = false" in r_off.init_js

    def test_no_external_src_or_href_in_markup(self):
        w = RssWidget()
        cfg = RssWidgetConfig(feed_url="https://feed.test/x.xml")
        r = w.render_html(cfg, _cell(), "abc")
        # The proxy URL must only live as a JS string literal, not as a
        # static src=/href= reference in the markup.
        assert "src=" not in r.html
        assert "href=" not in r.html
        assert "feed.test" not in r.html
