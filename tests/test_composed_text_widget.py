"""Tests for cms.composed.widgets.text.TextWidget."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

# Side-effect import: triggers global registry population
import cms.composed.widgets  # noqa: F401
from cms.composed.registry import WidgetRender, get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.text import TextWidget, TextWidgetConfig


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=1, colspan=1)


class TestTextWidgetConfig:
    def test_minimum_valid_config(self):
        c = TextWidgetConfig(text="hi")
        assert c.text == "hi"
        assert c.color == "#ffffff"
        assert c.font_size_px == 48
        assert c.font_family == "sans"

    def test_text_required(self):
        with pytest.raises(ValidationError):
            TextWidgetConfig()

    def test_text_min_length(self):
        with pytest.raises(ValidationError):
            TextWidgetConfig(text="")

    def test_text_max_length(self):
        with pytest.raises(ValidationError):
            TextWidgetConfig(text="x" * 4097)

    def test_text_max_length_boundary_ok(self):
        TextWidgetConfig(text="x" * 4096)

    def test_color_must_be_hex_6(self):
        for bad in ("red", "#fff", "#gggggg", "#FFFFFFF", "rgb(0,0,0)", ""):
            with pytest.raises(ValidationError):
                TextWidgetConfig(text="hi", color=bad)

    def test_color_uppercase_hex_accepted(self):
        c = TextWidgetConfig(text="hi", color="#ABCDEF")
        assert c.color == "#ABCDEF"

    def test_font_size_range_rejects_oob(self):
        for bad in (0, 7, 513, 1000):
            with pytest.raises(ValidationError):
                TextWidgetConfig(text="hi", font_size_px=bad)

    def test_font_size_range_accepts_boundary(self):
        TextWidgetConfig(text="hi", font_size_px=8)
        TextWidgetConfig(text="hi", font_size_px=512)

    def test_font_family_allowlist_accepts_known(self):
        for fam in ("sans", "serif", "mono"):
            assert (
                TextWidgetConfig(text="hi", font_family=fam).font_family == fam
            )

    def test_font_family_rejects_unknown(self):
        for bad in ("wingdings", "Arial", "", "Sans", "SANS"):
            with pytest.raises(ValidationError):
                TextWidgetConfig(text="hi", font_family=bad)

    def test_extra_fields_forbidden(self):
        with pytest.raises(ValidationError):
            TextWidgetConfig(text="hi", unknown_field="x")


class TestTextWidget:
    def test_default_config_validates(self):
        w = TextWidget()
        cfg = TextWidgetConfig.model_validate(w.default_config())
        assert cfg.text  # non-empty
        assert cfg.font_family in {"sans", "serif", "mono"}

    def test_editor_template_path_returned(self):
        # Contract: must return a non-empty path the Jinja loader
        # could (eventually) resolve.
        path = TextWidget().editor_template()
        assert path
        assert path.endswith(".html")

    def test_render_produces_instance_scoped_class(self):
        w = TextWidget()
        cfg = TextWidgetConfig(text="hi")
        instance_id = "11111111-1111-1111-1111-111111111111"
        out = w.render_html(cfg, _cell(), instance_id)
        assert isinstance(out, WidgetRender)
        scoped_class = f"cw-text-{instance_id}"
        assert scoped_class in out.html, (
            f"expected {scoped_class!r} in html: {out.html!r}"
        )
        assert f".{scoped_class} {{" in out.css, (
            f"expected scoped css rule for {scoped_class!r} in {out.css!r}"
        )

    def test_render_escapes_script_tag(self):
        w = TextWidget()
        cfg = TextWidgetConfig(text='<script>alert("xss")</script>')
        out = w.render_html(cfg, _cell(), "abc")
        # No raw <script> may survive into the body
        assert "<script>" not in out.html
        assert "</script>" not in out.html
        # Must appear escaped
        assert "&lt;script&gt;" in out.html
        assert "&lt;/script&gt;" in out.html

    def test_render_escapes_ampersand_and_quotes(self):
        w = TextWidget()
        cfg = TextWidgetConfig(text='A & B "C" \'D\'')
        out = w.render_html(cfg, _cell(), "abc")
        assert "&amp;" in out.html
        # html.escape() with default quote=True encodes " and '
        assert "&quot;" in out.html
        assert "&#x27;" in out.html

    def test_render_emits_color_size_and_font(self):
        w = TextWidget()
        cfg = TextWidgetConfig(
            text="hi",
            color="#abcdef",
            font_size_px=96,
            font_family="serif",
        )
        out = w.render_html(cfg, _cell(), "x")
        assert "color: #abcdef" in out.css
        assert "font-size: 96px" in out.css
        assert "Georgia" in out.css  # serif stack contains Georgia

    def test_render_two_instances_isolated(self):
        # Two instances with different IDs must produce non-colliding
        # CSS — proves the instance-scoping rule holds.
        w = TextWidget()
        cfg = TextWidgetConfig(text="hi")
        a = w.render_html(cfg, _cell(), "aaaa")
        b = w.render_html(cfg, _cell(), "bbbb")
        assert "cw-text-aaaa" in a.css
        assert "cw-text-bbbb" in b.css
        assert "cw-text-aaaa" not in b.css
        assert "cw-text-bbbb" not in a.css

    def test_render_no_referenced_assets_or_static_assets(self):
        # Text widget has no asset dependencies in 1A.
        w = TextWidget()
        cfg = TextWidgetConfig(text="hi")
        out = w.render_html(cfg, _cell(), "x")
        assert out.referenced_asset_ids == []
        assert out.static_assets == []
        assert out.js == ""
        assert out.init_js is None


class TestRegistration:
    def test_text_widget_registered_on_package_import(self):
        # The top-of-file `import cms.composed.widgets` must have
        # populated the global registry.
        reg = get_registry()
        assert reg.has("text"), (
            f"text widget missing from global registry; "
            f"registered slugs: {reg.slugs()}"
        )

    def test_registered_widget_is_a_text_widget(self):
        reg = get_registry()
        w = reg.get("text")
        assert isinstance(w, TextWidget)

    def test_re_import_does_not_double_register(self):
        # Re-running the registration block must be a no-op.  This
        # protects future cms.composed.__init__ changes that might
        # auto-import widgets from blowing up at startup.
        import importlib

        import cms.composed.widgets as widgets_pkg

        importlib.reload(widgets_pkg)
        reg = get_registry()
        assert reg.has("text")


class TestShrinkToFit:
    def test_default_off_is_byte_identical_to_no_field(self):
        # A config that omits shrink_to_fit and one that sets it False
        # must render byte-for-byte identical output — the legacy path.
        w = TextWidget()
        cfg_implicit = TextWidgetConfig(text="hi", color="#abcdef", font_size_px=64)
        cfg_explicit = TextWidgetConfig(
            text="hi", color="#abcdef", font_size_px=64, shrink_to_fit=False
        )
        a = w.render_html(cfg_implicit, _cell(), "x")
        b = w.render_html(cfg_explicit, _cell(), "x")
        assert a.html == b.html
        assert a.css == b.css
        assert a.js == b.js
        assert a.init_js == b.init_js

    def test_default_off_emits_no_autofit_code(self):
        w = TextWidget()
        cfg = TextWidgetConfig(text="hi", shrink_to_fit=False)
        out = w.render_html(cfg, _cell(), "x")
        assert "__cwFit" not in out.html
        assert "__cwFit" not in out.css
        assert "__cwFit" not in out.js
        assert out.js == ""
        assert out.init_js is None
        # The legacy box carries the literal font-size; no inner element.
        assert "cw-text-inner-" not in out.html

    def test_on_path_emits_autofit_js_and_init(self):
        w = TextWidget()
        cfg = TextWidgetConfig(text="hi", font_size_px=64, shrink_to_fit=True)
        out = w.render_html(cfg, _cell(), "abcd")
        # Shared fit helper is emitted in js.
        assert "window.__cwFit" in out.js
        assert "window.__cwFitObserve" in out.js
        # Per-instance init wires the inner element to the observer.
        inner_id = "cw-text-inner-abcd"
        assert inner_id in out.html
        assert inner_id in out.init_js
        assert "__cwFitObserve" in out.init_js
        # The starting (pre-JS) size is preserved on the inner element.
        assert "font-size: 64px" in out.css

    def test_on_path_still_escapes_text(self):
        w = TextWidget()
        cfg = TextWidgetConfig(
            text='<script>alert(1)</script>', shrink_to_fit=True
        )
        out = w.render_html(cfg, _cell(), "x")
        assert "<script>" not in out.html
        assert "&lt;script&gt;" in out.html

    def test_default_config_includes_shrink_to_fit_false(self):
        assert TextWidget().default_config()["shrink_to_fit"] is False
