"""Tests for cms.composed.widgets.iframe.IframeWidget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import cms.composed.widgets  # noqa: F401
from cms.composed.registry import BundleContext, get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.iframe import (
    IframeWidget,
    IframeWidgetConfig,
    _DEFAULT_URL,
    _js_str,
)


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


class TestIframeWidgetConfig:
    def test_defaults(self):
        c = IframeWidgetConfig()
        assert c.url == _DEFAULT_URL
        assert c.allow_scripts is True
        assert c.refresh_seconds == 0
        assert c.background_color == "#000000"
        assert c.unavailable_text == "Content unavailable"

    def test_default_config_matches_schema(self):
        # A freshly-placed widget must immediately be a valid config.
        w = IframeWidget()
        IframeWidgetConfig(**w.default_config())

    def test_url_must_be_http(self):
        IframeWidgetConfig(url="http://example.com/")
        IframeWidgetConfig(url="https://example.com/dash")
        with pytest.raises(ValidationError):
            IframeWidgetConfig(url="ftp://example.com/x")
        with pytest.raises(ValidationError):
            IframeWidgetConfig(url="javascript:alert(1)")
        with pytest.raises(ValidationError):
            IframeWidgetConfig(url="data:text/html,<h1>hi</h1>")
        with pytest.raises(ValidationError):
            IframeWidgetConfig(url="not-a-url")

    def test_url_stripped(self):
        c = IframeWidgetConfig(url="  https://example.com/x  ")
        assert c.url == "https://example.com/x"

    def test_url_max_length(self):
        with pytest.raises(ValidationError):
            IframeWidgetConfig(url="https://e.com/" + "a" * 2048)

    def test_refresh_floor(self):
        IframeWidgetConfig(refresh_seconds=0)
        IframeWidgetConfig(refresh_seconds=60)
        IframeWidgetConfig(refresh_seconds=86400)
        with pytest.raises(ValidationError):
            IframeWidgetConfig(refresh_seconds=30)
        with pytest.raises(ValidationError):
            IframeWidgetConfig(refresh_seconds=59)
        with pytest.raises(ValidationError):
            IframeWidgetConfig(refresh_seconds=86401)
        with pytest.raises(ValidationError):
            IframeWidgetConfig(refresh_seconds=-1)

    def test_background_must_be_hex_6(self):
        IframeWidgetConfig(background_color="#abcdef")
        with pytest.raises(ValidationError):
            IframeWidgetConfig(background_color="black")
        with pytest.raises(ValidationError):
            IframeWidgetConfig(background_color="#fff")

    def test_unavailable_text_max_length(self):
        IframeWidgetConfig(unavailable_text="x" * 120)
        with pytest.raises(ValidationError):
            IframeWidgetConfig(unavailable_text="x" * 121)

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            IframeWidgetConfig(api_key="secret")  # type: ignore[call-arg]


class TestIframeWidgetRegistry:
    def test_registered(self):
        w = get_registry().get("iframe")
        assert isinstance(w, IframeWidget)
        assert w.slug == "iframe"
        assert w.display_name == "Web Embed"

    def test_no_declared_assets(self):
        w = IframeWidget()
        assert w.declared_asset_ids(IframeWidgetConfig()) == []

    def test_validate_semantic_passes(self):
        w = IframeWidget()
        assert w.validate_semantic(IframeWidgetConfig()) == []


class TestJsStr:
    def test_escapes_angle_and_amp(self):
        out = _js_str("a</script>&b")
        assert "</script>" not in out
        assert "\\u003c" in out
        assert "\\u003e" in out
        assert "\\u0026" in out


class TestIframeWidgetRender:
    def test_html_and_css_are_instance_scoped(self):
        w = IframeWidget()
        cfg = IframeWidgetConfig()
        r1 = w.render_html(cfg, _cell(), "inst-A")
        r2 = w.render_html(cfg, _cell(), "inst-B")
        assert "cw-iframe-inst-A" in r1.html
        assert "cw-iframe-inst-A" in r1.css
        assert "cw-iframe-inst-B" in r2.html
        assert "inst-B" not in r1.html
        assert "inst-A" not in r2.html

    def test_iframe_has_no_src_attribute(self):
        # The whole point: the URL never appears as a static src= attr;
        # it is injected at runtime via init_js to satisfy the bundle's
        # no-external-reference invariant.
        w = IframeWidget()
        cfg = IframeWidgetConfig(url="https://frame.test/dash")
        r = w.render_html(cfg, _cell(), "abc")
        assert "<iframe" in r.html
        assert "src=" not in r.html
        assert "href=" not in r.html
        assert "frame.test" not in r.html

    def test_sandbox_attr_present(self):
        w = IframeWidget()
        r = w.render_html(IframeWidgetConfig(allow_scripts=True), _cell(), "abc")
        assert 'sandbox="allow-same-origin allow-scripts"' in r.html
        # Never grant top-navigation / popups / forms.
        assert "allow-top-navigation" not in r.html
        assert "allow-popups" not in r.html
        assert "allow-forms" not in r.html

    def test_sandbox_omits_scripts_when_disabled(self):
        w = IframeWidget()
        r = w.render_html(IframeWidgetConfig(allow_scripts=False), _cell(), "abc")
        assert 'sandbox="allow-same-origin"' in r.html
        assert "allow-scripts" not in r.html

    def test_referrer_policy_no_referrer(self):
        w = IframeWidget()
        r = w.render_html(IframeWidgetConfig(), _cell(), "abc")
        assert 'referrerpolicy="no-referrer"' in r.html

    def test_init_js_sets_src_at_runtime(self):
        w = IframeWidget()
        cfg = IframeWidgetConfig(url="https://frame.test/dash")
        r = w.render_html(cfg, _cell(), "abc")
        assert r.init_js is not None
        js = r.init_js
        assert "frame.src = URL" in js
        # The URL is embedded via _js_str (HTML-escaped "/" stays, but ":"
        # and "//" are present as a JS literal, not a static attribute).
        assert "frame.test/dash" in js

    def test_init_js_no_refresh_when_zero(self):
        w = IframeWidget()
        r = w.render_html(IframeWidgetConfig(refresh_seconds=0), _cell(), "abc")
        assert r.init_js is not None
        assert "var REFRESH_MS = 0;" in r.init_js

    def test_init_js_refresh_ms_baked_in(self):
        w = IframeWidget()
        r = w.render_html(IframeWidgetConfig(refresh_seconds=120), _cell(), "abc")
        assert r.init_js is not None
        assert "var REFRESH_MS = 120000;" in r.init_js
        assert "setInterval(" in r.init_js

    def test_unavailable_text_escaped_in_markup(self):
        w = IframeWidget()
        cfg = IframeWidgetConfig(unavailable_text="<b>Down</b> & out")
        r = w.render_html(cfg, _cell(), "abc")
        assert "&lt;b&gt;Down&lt;/b&gt; &amp; out" in r.html
        assert "<b>Down</b>" not in r.html
        # The fallback text is never passed into JS.
        assert r.init_js is not None
        assert "Down" not in r.init_js

    def test_background_color_applied(self):
        w = IframeWidget()
        r = w.render_html(
            IframeWidgetConfig(background_color="#123456"), _cell(), "abc"
        )
        assert "#123456" in r.css

    def test_ctx_is_ignored(self):
        # iframe loads directly; cms_base_url must not leak into output.
        w = IframeWidget()
        ctx = BundleContext(cms_base_url="https://cms.example.org")
        r = w.render_html(IframeWidgetConfig(), _cell(), "abc", ctx)
        assert "cms.example.org" not in (r.init_js or "")
        assert "cms.example.org" not in r.html
