"""Tests for ``cms.composed.bundle.build_bundle``.

Phase 1A scope:
- Trivial empty layout → valid minimal HTML
- Single text widget layout → bundle contains the rendered HTML/CSS
- Bundle has zero external src=/href= refs (anchored on http/https
  schemes — data: URIs are allowed)
- Deterministic: same layout → same SHA on rebuild
- Validation errors raise BundleValidationError
- Source asset IDs flow through to the BuiltBundle result
"""

from __future__ import annotations

import re
import uuid

import pytest
from pydantic import BaseModel, Field

# Import to trigger TextWidget auto-registration into the global
# registry; the bundle builder uses get_registry() by default.
import cms.composed.widgets  # noqa: F401
from cms.composed.bundle import (
    BuiltBundle,
    BundleValidationError,
    build_bundle,
)
from cms.composed.registry import (
    Widget,
    WidgetRegistry,
    WidgetRender,
    get_registry,
)
from cms.composed.schema import (
    Background,
    Cell,
    Layout,
    WidgetInstance,
    empty_layout,
)


# ── Test helpers ─────────────────────────────────────────────────────


def _text_layout(text: str = "Hello", color: str = "#ff0000") -> Layout:
    return Layout(
        widgets=[
            WidgetInstance(
                id=uuid.UUID("11111111-1111-1111-1111-111111111111"),
                type="text",
                cell=Cell(row=1, col=1, rowspan=2, colspan=3),
                config={"text": text, "color": color},
            ),
        ],
    )


# ── Tests: structure / minimal cases ─────────────────────────────────


class TestEmptyLayout:
    def test_empty_layout_produces_valid_doc(self):
        result = build_bundle(empty_layout())

        assert isinstance(result, BuiltBundle)
        html = result.html_bytes.decode("utf-8")
        assert html.startswith("<!doctype html>")
        assert "<html" in html and "</html>" in html
        assert "cw-canvas" in html
        # No widget cells.
        assert "data-widget-instance" not in html

    def test_empty_layout_no_referenced_asset_ids(self):
        result = build_bundle(empty_layout())
        assert result.source_asset_ids == []


class TestTextWidgetBundle:
    def test_renders_widget_content_in_doc(self):
        layout = _text_layout(text="Hello, world", color="#abcdef")
        result = build_bundle(layout)
        html = result.html_bytes.decode("utf-8")

        assert "Hello, world" in html
        # CSS color is propagated.
        assert "#abcdef" in html
        # Instance-scoped class survives into the document.
        assert "cw-text-11111111-1111-1111-1111-111111111111" in html
        # Cell wrapper sets grid-area placement (row=1 colspan=3).
        assert "grid-row: 1 / span 2" in html
        assert "grid-column: 1 / span 3" in html

    def test_html_escapes_user_text(self):
        layout = _text_layout(text="<script>alert(1)</script>")
        result = build_bundle(layout)
        html = result.html_bytes.decode("utf-8")

        # Raw script tag from user input must never appear.
        assert "<script>alert(1)</script>" not in html
        # Escaped form should be present in the body.
        assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html

    def test_background_color_threaded_through(self):
        layout = Layout(
            background=Background(color="#102030"),
            widgets=[],
        )
        result = build_bundle(layout)
        assert b"#102030" in result.html_bytes


class TestNoExternalReferences:
    """Bundles ship to devices and must be fully offline-tolerant."""

    EXTERNAL_RE = re.compile(
        # Match any src= or href= whose value starts with http(s)/// (raw
        # protocol-relative URLs).  data: URIs are explicitly allowed.
        r'''(?:src|href)\s*=\s*["'](?:https?:|//)''',
        re.IGNORECASE,
    )

    def test_text_widget_bundle_has_no_external_refs(self):
        layout = _text_layout()
        html = build_bundle(layout).html_bytes.decode("utf-8")

        match = self.EXTERNAL_RE.search(html)
        assert match is None, f"unexpected external reference: {match!r}"

    def test_empty_bundle_has_no_external_refs(self):
        html = build_bundle(empty_layout()).html_bytes.decode("utf-8")
        assert self.EXTERNAL_RE.search(html) is None


class TestDeterminism:
    def test_same_layout_produces_same_sha_and_bytes(self):
        layout1 = _text_layout(text="hello", color="#ffffff")
        layout2 = _text_layout(text="hello", color="#ffffff")

        a = build_bundle(layout1)
        b = build_bundle(layout2)

        assert a.html_bytes == b.html_bytes
        assert a.sha256_hex == b.sha256_hex

    def test_different_text_changes_sha(self):
        a = build_bundle(_text_layout(text="hello"))
        b = build_bundle(_text_layout(text="world"))
        assert a.sha256_hex != b.sha256_hex


# ── Tests: validation handoff ───────────────────────────────────────


class TestValidationHandoff:
    def test_out_of_bounds_layout_raises(self):
        layout = Layout(
            widgets=[
                WidgetInstance(
                    id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
                    type="text",
                    # rowspan=8 starting at row=2 pushes past 8-row grid.
                    cell=Cell(row=2, col=1, rowspan=8, colspan=1),
                    config={"text": "x"},
                ),
            ],
        )
        with pytest.raises(BundleValidationError) as ei:
            build_bundle(layout)
        assert any(e.code == "cell_out_of_bounds" for e in ei.value.errors)

    def test_unknown_widget_type_raises(self):
        # Use an isolated registry with no widgets registered.
        empty_reg = WidgetRegistry()
        layout = Layout(
            widgets=[
                WidgetInstance(
                    id=uuid.UUID("33333333-3333-3333-3333-333333333333"),
                    type="text",  # registered globally but NOT in our isolated reg
                    cell=Cell(row=1, col=1),
                    config={"text": "x"},
                ),
            ],
        )
        with pytest.raises(BundleValidationError) as ei:
            build_bundle(layout, registry=empty_reg)
        assert any(e.code == "unknown_widget_type" for e in ei.value.errors)


# ── Tests: static-asset & init_js plumbing (synthetic widget) ───────


class _StaticAssetConfig(BaseModel):
    msg: str = Field(default="x")


class _StaticAssetWidget(Widget):
    slug = "static_asset_test"
    display_name = "Static Asset Test"
    icon = "x"
    ConfigSchema = _StaticAssetConfig
    config_version = 1

    def default_config(self):
        return {"msg": "x"}

    def editor_template(self):
        return "n/a"

    def render_html(self, config, cell, instance_id):
        return WidgetRender(
            html=f'<span id="x-{instance_id}">x</span>',
            css=f"#x-{instance_id} {{color:red;}}",
            js="window.__sharedHelper = function(){return 1;};",
            init_js=f"document.getElementById('x-' + instanceId).dataset.ok='1';",
        )


class TestStaticAssetsAndInitJs:
    def test_init_js_is_wrapped_in_domcontentloaded_with_instance_id(self):
        reg = WidgetRegistry()
        reg.register(_StaticAssetWidget())

        inst_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
        layout = Layout(
            widgets=[
                WidgetInstance(
                    id=inst_id,
                    type="static_asset_test",
                    cell=Cell(row=1, col=1),
                    config={"msg": "x"},
                ),
            ],
        )
        html = build_bundle(layout, registry=reg).html_bytes.decode("utf-8")

        assert "DOMContentLoaded" in html
        # init_js wrapped in a function and called with the instance id literal.
        assert str(inst_id) in html

    def test_shared_js_is_deduplicated(self):
        reg = WidgetRegistry()
        reg.register(_StaticAssetWidget())

        layout = Layout(
            widgets=[
                WidgetInstance(
                    id=uuid.UUID("55555555-5555-5555-5555-555555555555"),
                    type="static_asset_test",
                    cell=Cell(row=1, col=1),
                    config={"msg": "a"},
                ),
                WidgetInstance(
                    id=uuid.UUID("66666666-6666-6666-6666-666666666666"),
                    type="static_asset_test",
                    cell=Cell(row=2, col=1),
                    config={"msg": "b"},
                ),
            ],
        )
        html = build_bundle(layout, registry=reg).html_bytes.decode("utf-8")

        # The shared helper string should appear exactly once.
        assert html.count("window.__sharedHelper") == 1


class TestRegistryWiring:
    def test_default_registry_is_global(self):
        # Sanity check: when no registry is passed, the builder uses
        # get_registry(), and the text widget is registered there via
        # the import at the top of this module.
        assert get_registry().has("text")
        # And it actually works end-to-end (regression).
        result = build_bundle(_text_layout())
        assert b"cw-text-" in result.html_bytes
