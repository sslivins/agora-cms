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
from typing import ClassVar

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

    def test_weather_widget_bundle_has_no_external_attr_refs(self):
        # The weather widget makes a runtime fetch, but the Open-Meteo
        # URL must only ever exist as a JS string literal — never as a
        # src=/href= attribute that a strict offline check would flag.
        layout = Layout(
            widgets=[
                WidgetInstance(
                    id=uuid.UUID("22222222-2222-2222-2222-222222222222"),
                    type="weather",
                    cell=Cell(row=1, col=1, rowspan=2, colspan=3),
                    config={},
                ),
            ],
        )
        html = build_bundle(layout).html_bytes.decode("utf-8")
        # The forecast endpoint is present (baked into init_js)…
        assert "open-meteo.com/v1/forecast" in html
        # …but never as an external src=/href= attribute.
        match = self.EXTERNAL_RE.search(html)
        assert match is None, f"unexpected external reference: {match!r}"


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


class TestZIndexStacking:
    """Overlapping widgets stack by ``layout.widgets`` array order:
    later entries paint on top via an explicit z-index."""

    def _two_overlapping(self) -> Layout:
        return Layout(
            widgets=[
                WidgetInstance(
                    id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                    type="text",
                    cell=Cell(row=1, col=1, rowspan=2, colspan=2),
                    config={"text": "back"},
                ),
                WidgetInstance(
                    id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                    type="text",
                    cell=Cell(row=1, col=1, rowspan=2, colspan=2),
                    config={"text": "front"},
                ),
            ],
        )

    def test_overlapping_widgets_build_without_error(self):
        # Overlap used to fail validation; it must now succeed.
        result = build_bundle(self._two_overlapping())
        assert isinstance(result, BuiltBundle)

    def test_array_order_maps_to_ascending_z_index(self):
        html = build_bundle(self._two_overlapping()).html_bytes.decode("utf-8")
        i_back = html.index("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
        i_front = html.index("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
        # First array entry renders first and gets the lower z-index.
        assert i_back < i_front
        assert "z-index: 0;" in html
        assert "z-index: 1;" in html
        # The lower z-index belongs to the earlier (back) widget.
        z0 = html.index("z-index: 0;")
        z1 = html.index("z-index: 1;")
        assert z0 < z1


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

    def render_html(self, config, cell, instance_id, ctx=None):
        del ctx
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


# ── Tests: BundleContext / asset_loader contract (Phase 1B) ──────────


class _AssetConfig(BaseModel):
    """Synthetic 'declares-an-asset' widget config."""

    asset_id: uuid.UUID


class _AssetWidget(Widget):
    """Synthetic widget that both declares AND references an asset id.

    Lets us drive the bundle's pre-fetch / scoped-context plumbing
    without depending on the real ImageWidget implementation.
    """

    slug = "asset_test"
    display_name = "Asset Test"
    icon = "x"
    ConfigSchema = _AssetConfig
    config_version = 1

    def default_config(self):
        return {"asset_id": "00000000-0000-0000-0000-000000000000"}

    def editor_template(self):
        return "n/a"

    def declared_asset_ids(self, config):
        return [config.asset_id]

    def render_html(self, config, cell, instance_id, ctx=None):
        assert ctx is not None, "_AssetWidget requires a BundleContext"
        blob = ctx.asset_bytes[config.asset_id]
        mime = ctx.asset_mimes[config.asset_id]
        return WidgetRender(
            html=f'<span id="a-{instance_id}" data-mime="{mime}" data-len="{len(blob)}">x</span>',
            referenced_asset_ids=[config.asset_id],
        )


class _BadReferenceWidget(Widget):
    """Synthetic widget that 'forgets' to declare what it references.

    Used to verify the bundle builder catches the contract violation.
    """

    slug = "bad_ref_test"
    display_name = "Bad Ref"
    icon = "x"
    ConfigSchema = _AssetConfig
    config_version = 1

    def default_config(self):
        return {"asset_id": "00000000-0000-0000-0000-000000000000"}

    def editor_template(self):
        return "n/a"

    def declared_asset_ids(self, config):
        # Intentionally does NOT declare config.asset_id even though
        # render_html references it below.
        return []

    def render_html(self, config, cell, instance_id, ctx=None):
        del ctx
        return WidgetRender(
            html=f'<span id="bad-{instance_id}">x</span>',
            referenced_asset_ids=[config.asset_id],
        )


class _LeakyContextWidget(Widget):
    """Widget that records which asset ids it can see in its context.

    Lets the scoping test assert that a widget's BundleContext only
    contains assets *this* widget declared, not ones declared by
    neighbouring widgets in the same layout.
    """

    slug = "leaky_ctx_test"
    display_name = "Leaky Ctx"
    icon = "x"
    ConfigSchema = _AssetConfig
    config_version = 1

    # Class-level capture point so test code can inspect what the
    # builder handed each instance.
    seen: ClassVar = []  # type: ignore[var-annotated]

    def default_config(self):
        return {"asset_id": "00000000-0000-0000-0000-000000000000"}

    def editor_template(self):
        return "n/a"

    def declared_asset_ids(self, config):
        return [config.asset_id]

    def render_html(self, config, cell, instance_id, ctx=None):
        assert ctx is not None
        self.__class__.seen.append((instance_id, sorted(ctx.asset_bytes.keys())))
        return WidgetRender(
            html=f'<span id="leaky-{instance_id}">x</span>',
            referenced_asset_ids=[config.asset_id],
        )


class TestBundleAssetLoaderContract:
    def test_missing_loader_raises_when_widgets_declare_assets(self):
        from cms.composed.bundle import MissingAssetLoaderError

        reg = WidgetRegistry()
        reg.register(_AssetWidget())
        aid = uuid.uuid4()
        layout = Layout(
            widgets=[
                WidgetInstance(
                    id=uuid.uuid4(),
                    type="asset_test",
                    cell=Cell(row=1, col=1),
                    config={"asset_id": str(aid)},
                ),
            ],
        )
        with pytest.raises(MissingAssetLoaderError):
            build_bundle(layout, registry=reg)  # no asset_loader

    def test_no_loader_needed_when_no_assets_declared(self):
        # The text-only layout never declares any assets; building
        # without a loader must keep working (1A regression check).
        result = build_bundle(_text_layout())
        assert isinstance(result, BuiltBundle)

    def test_loader_called_once_per_unique_id(self):
        reg = WidgetRegistry()
        reg.register(_AssetWidget())
        aid = uuid.uuid4()
        # Same asset referenced by two widgets.
        layout = Layout(
            widgets=[
                WidgetInstance(
                    id=uuid.uuid4(),
                    type="asset_test",
                    cell=Cell(row=1, col=1),
                    config={"asset_id": str(aid)},
                ),
                WidgetInstance(
                    id=uuid.uuid4(),
                    type="asset_test",
                    cell=Cell(row=2, col=1),
                    config={"asset_id": str(aid)},
                ),
            ],
        )
        calls: list[uuid.UUID] = []

        def loader(asset_id):
            calls.append(asset_id)
            return (b"BYTES", "image/png")

        result = build_bundle(layout, registry=reg, asset_loader=loader)
        assert calls == [aid]  # exactly one call, despite two widgets
        # Both widgets still received the bytes (rendered with the
        # data-len attribute) — verify both spans are present.
        html_text = result.html_bytes.decode("utf-8")
        assert html_text.count('data-len="5"') == 2

    def test_undeclared_referenced_asset_raises(self):
        reg = WidgetRegistry()
        reg.register(_BadReferenceWidget())
        aid = uuid.uuid4()
        layout = Layout(
            widgets=[
                WidgetInstance(
                    id=uuid.uuid4(),
                    type="bad_ref_test",
                    cell=Cell(row=1, col=1),
                    config={"asset_id": str(aid)},
                ),
            ],
        )

        # No loader needed because the widget declares nothing — the
        # builder catches the violation in pass 2.
        with pytest.raises(BundleValidationError) as ei:
            build_bundle(layout, registry=reg)
        assert any(
            e.code == "undeclared_referenced_asset" for e in ei.value.errors
        )

    def test_each_widget_only_sees_its_own_declared_assets(self):
        _LeakyContextWidget.seen.clear()
        reg = WidgetRegistry()
        reg.register(_LeakyContextWidget())

        aid_a = uuid.uuid4()
        aid_b = uuid.uuid4()
        layout = Layout(
            widgets=[
                WidgetInstance(
                    id=uuid.UUID("aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"),
                    type="leaky_ctx_test",
                    cell=Cell(row=1, col=1),
                    config={"asset_id": str(aid_a)},
                ),
                WidgetInstance(
                    id=uuid.UUID("bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"),
                    type="leaky_ctx_test",
                    cell=Cell(row=2, col=1),
                    config={"asset_id": str(aid_b)},
                ),
            ],
        )

        def loader(asset_id):
            return (b"x", "image/png")

        build_bundle(layout, registry=reg, asset_loader=loader)

        # Two widgets rendered, each saw only its own asset id.
        assert len(_LeakyContextWidget.seen) == 2
        for _instance_id, seen_ids in _LeakyContextWidget.seen:
            assert len(seen_ids) == 1
        # And the two widgets' contexts were genuinely different.
        sets = {tuple(s) for _, s in _LeakyContextWidget.seen}
        assert sets == {(aid_a,), (aid_b,)}

    def test_source_asset_ids_flow_through_to_built_bundle(self):
        reg = WidgetRegistry()
        reg.register(_AssetWidget())
        aid_a = uuid.uuid4()
        aid_b = uuid.uuid4()
        layout = Layout(
            widgets=[
                WidgetInstance(
                    id=uuid.uuid4(),
                    type="asset_test",
                    cell=Cell(row=1, col=1),
                    config={"asset_id": str(aid_a)},
                ),
                WidgetInstance(
                    id=uuid.uuid4(),
                    type="asset_test",
                    cell=Cell(row=2, col=1),
                    config={"asset_id": str(aid_b)},
                ),
            ],
        )

        def loader(asset_id):
            return (b"x", "image/png")

        result = build_bundle(layout, registry=reg, asset_loader=loader)
        # Both ids surface in source_asset_ids (order = first-appearance).
        assert set(result.source_asset_ids) == {aid_a, aid_b}
