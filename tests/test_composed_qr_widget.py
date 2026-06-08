"""Tests for cms.composed.widgets.qr.QrWidget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

import cms.composed.widgets  # noqa: F401
from cms.composed.registry import get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.qr import QrWidget, QrWidgetConfig


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


class TestQrWidgetConfig:
    def test_defaults(self):
        c = QrWidgetConfig(url="https://example.com")
        assert c.url == "https://example.com"
        assert c.error_correction == "M"
        assert c.foreground == "#000000"
        assert c.background == "#ffffff"
        assert c.quiet_zone is True

    def test_error_correction_allowlist(self):
        for ok in ("L", "M", "Q", "H"):
            QrWidgetConfig(url="https://example.com", error_correction=ok)
        with pytest.raises(ValidationError):
            QrWidgetConfig(
                url="https://example.com",
                error_correction="Z",  # type: ignore[arg-type]
            )

    def test_url_required_and_non_blank(self):
        with pytest.raises(ValidationError):
            QrWidgetConfig()  # type: ignore[call-arg]
        with pytest.raises(ValidationError):
            QrWidgetConfig(url="   ")

    def test_url_scheme_allowlist(self):
        QrWidgetConfig(url="http://example.com")
        QrWidgetConfig(url="https://example.com/path?a=1")
        for bad in (
            "javascript:alert(1)",
            "data:text/html,<h1>x</h1>",
            "ftp://example.com",
            "example.com",
        ):
            with pytest.raises(ValidationError):
                QrWidgetConfig(url=bad)

    def test_url_stripped(self):
        c = QrWidgetConfig(url="  https://example.com  ")
        assert c.url == "https://example.com"

    def test_url_length_capped(self):
        long_url = "https://example.com/" + ("a" * 2000)
        with pytest.raises(ValidationError):
            QrWidgetConfig(url=long_url)

    def test_foreground_must_be_hex_6(self):
        with pytest.raises(ValidationError):
            QrWidgetConfig(url="https://example.com", foreground="black")

    def test_background_must_be_hex_6(self):
        with pytest.raises(ValidationError):
            QrWidgetConfig(url="https://example.com", background="white")

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            QrWidgetConfig(
                url="https://example.com",
                center_label="hi",  # type: ignore[call-arg]
            )


class TestQrWidgetRegistry:
    def test_registered(self):
        w = get_registry().get("qr")
        assert isinstance(w, QrWidget)
        assert w.slug == "qr"

    def test_default_config_validates(self):
        w = QrWidget()
        # default_config must round-trip through the config schema.
        QrWidgetConfig(**w.default_config())


class TestQrWidgetRender:
    def test_no_declared_assets(self):
        w = QrWidget()
        cfg = QrWidgetConfig(url="https://example.com")
        assert w.declared_asset_ids(cfg) == []

    def test_render_emits_inline_svg(self):
        w = QrWidget()
        cfg = QrWidgetConfig(url="https://example.com")
        r = w.render_html(cfg, _cell(), "abc")
        assert "<svg" in r.html
        assert "viewBox" in r.html

    def test_no_init_js_or_assets(self):
        w = QrWidget()
        cfg = QrWidgetConfig(url="https://example.com")
        r = w.render_html(cfg, _cell(), "abc")
        assert r.init_js is None
        assert r.js == ""
        assert r.static_assets == []
        assert r.referenced_asset_ids == []

    def test_no_external_references(self):
        w = QrWidget()
        cfg = QrWidgetConfig(url="https://example.com")
        r = w.render_html(cfg, _cell(), "abc")
        # Fully self-contained: no external resource loads.
        assert "src=" not in r.html
        assert "href=" not in r.html
        assert "<script" not in r.html

    def test_html_and_css_are_instance_scoped(self):
        w = QrWidget()
        cfg = QrWidgetConfig(url="https://example.com")

        r1 = w.render_html(cfg, _cell(), "inst-A")
        r2 = w.render_html(cfg, _cell(), "inst-B")

        assert "cw-qr-inst-A" in r1.html
        assert "cw-qr-inst-A" in r1.css
        assert "cw-qr-inst-B" in r2.html
        # No cross-contamination
        assert "cw-qr-inst-B" not in r1.html
        assert "cw-qr-inst-A" not in r2.html

    def test_render_is_deterministic(self):
        w = QrWidget()
        cfg = QrWidgetConfig(url="https://example.com", error_correction="Q")
        r1 = w.render_html(cfg, _cell(), "abc")
        r2 = w.render_html(cfg, _cell(), "abc")
        assert r1.html == r2.html
        assert r1.css == r2.css

    def test_colors_propagate(self):
        w = QrWidget()
        cfg = QrWidgetConfig(
            url="https://example.com",
            foreground="#123456",
            background="#445566",
        )
        r = w.render_html(cfg, _cell(), "abc")
        # foreground colours the dark modules in the SVG; background
        # colours the wrapper gutter via CSS.  (segno collapses 6-hex to
        # 3-hex when each channel's digits repeat, so we pick a value
        # that survives verbatim.)
        assert "#123456" in r.html
        assert "background: #445566" in r.css

    def test_quiet_zone_changes_matrix_border(self):
        w = QrWidget()
        url = "https://example.com"
        r_on = w.render_html(
            QrWidgetConfig(url=url, quiet_zone=True), _cell(), "a"
        )
        r_off = w.render_html(
            QrWidgetConfig(url=url, quiet_zone=False), _cell(), "b"
        )
        # The quiet zone adds a 4-module border, enlarging the viewBox.
        assert r_on.html != r_off.html

    def test_validate_semantic_ok(self):
        w = QrWidget()
        cfg = QrWidgetConfig(url="https://example.com")
        assert w.validate_semantic(cfg) == []
