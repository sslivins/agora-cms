"""Round-2 extensibility tests — text/ticker variants + contract guardrail.

These tests prove the Phase 1B plugin contract supports adding new
widget variants (font choices, text styling toggles, animation modes)
purely via config-schema + render additions, with zero changes to:

* ``cms/composed/registry.py``  — the plugin contract surface
* ``cms/composed/bundle.py``    — the bundle-builder pipeline
* ``cms/composed/publish.py``   — the asset-pre-fetch wiring

The final test in this file (``test_contract_surface_pinned``) is a
**guardrail**: it asserts the public surface of the registry contract
hasn't drifted.  If a future PR changes Widget.render_html's signature
or BundleContext's fields, this test fails — forcing the author to
either revert or explicitly update this pin (making the contract
change visible in code review).
"""

from __future__ import annotations

import inspect
import uuid
from dataclasses import fields, is_dataclass

import pytest
from pydantic import ValidationError

import cms.composed.widgets  # noqa: F401  (triggers widget registration)
from cms.composed.bundle import (
    AssetLoader,
    BundleValidationError,
    MissingAssetLoaderError,
    build_bundle,
)
from cms.composed.registry import (
    BundleContext,
    Widget,
    WidgetRender,
    WidgetRegistry,
    get_registry,
)
from cms.composed.schema import Cell, Layout, WidgetInstance
from cms.composed.widgets.text import TextWidget, TextWidgetConfig
from cms.composed.widgets.ticker import TickerWidget, TickerWidgetConfig


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=4)


# ────────────────────────────────────────────────────────────────────
# Text widget — bold / italic / new font slugs
# ────────────────────────────────────────────────────────────────────


class TestTextWidgetVariants:
    def test_bold_and_italic_default_false(self):
        c = TextWidgetConfig(text="hi")
        assert c.bold is False
        assert c.italic is False

    def test_default_config_includes_new_keys(self):
        d = TextWidget().default_config()
        assert d["bold"] is False
        assert d["italic"] is False

    def test_bold_emits_font_weight_700(self):
        cfg = TextWidgetConfig(text="hi", bold=True)
        out = TextWidget().render_html(cfg, _cell(), "inst1")
        assert "font-weight: 700" in out.css
        assert "font-weight: 400" not in out.css

    def test_not_bold_emits_font_weight_400(self):
        cfg = TextWidgetConfig(text="hi", bold=False)
        out = TextWidget().render_html(cfg, _cell(), "inst1")
        assert "font-weight: 400" in out.css

    def test_italic_emits_font_style_italic(self):
        cfg = TextWidgetConfig(text="hi", italic=True)
        out = TextWidget().render_html(cfg, _cell(), "inst1")
        assert "font-style: italic" in out.css
        assert "font-style: normal" not in out.css

    def test_not_italic_emits_font_style_normal(self):
        cfg = TextWidgetConfig(text="hi", italic=False)
        out = TextWidget().render_html(cfg, _cell(), "inst1")
        assert "font-style: normal" in out.css

    def test_bold_and_italic_combine(self):
        cfg = TextWidgetConfig(text="hi", bold=True, italic=True)
        out = TextWidget().render_html(cfg, _cell(), "inst1")
        assert "font-weight: 700" in out.css
        assert "font-style: italic" in out.css

    @pytest.mark.parametrize("slug", ["sans", "serif", "mono", "display", "handwritten"])
    def test_new_font_slugs_validate(self, slug):
        c = TextWidgetConfig(text="hi", font_family=slug)
        assert c.font_family == slug

    def test_display_font_emits_impact_stack(self):
        cfg = TextWidgetConfig(text="hi", font_family="display")
        out = TextWidget().render_html(cfg, _cell(), "inst1")
        assert "Impact" in out.css

    def test_handwritten_font_emits_cursive_fallback(self):
        cfg = TextWidgetConfig(text="hi", font_family="handwritten")
        out = TextWidget().render_html(cfg, _cell(), "inst1")
        assert "cursive" in out.css

    def test_unknown_font_still_rejected(self):
        with pytest.raises(ValidationError):
            TextWidgetConfig(text="hi", font_family="not-a-font")


# ────────────────────────────────────────────────────────────────────
# Ticker widget — bounce mode
# ────────────────────────────────────────────────────────────────────


class TestTickerWidgetBounceMode:
    def test_mode_defaults_to_scroll(self):
        c = TickerWidgetConfig(text="hi")
        assert c.mode == "scroll"

    def test_default_config_includes_mode(self):
        d = TickerWidget().default_config()
        assert d["mode"] == "scroll"

    def test_bounce_mode_validates(self):
        c = TickerWidgetConfig(text="hi", mode="bounce")
        assert c.mode == "bounce"

    def test_unknown_mode_rejected(self):
        with pytest.raises(ValidationError):
            TickerWidgetConfig(text="hi", mode="zigzag")  # type: ignore[arg-type]

    def test_scroll_left_emits_normal_animation(self):
        cfg = TickerWidgetConfig(text="hi", mode="scroll", direction="left")
        out = TickerWidget().render_html(cfg, _cell(), "abc")
        assert "infinite normal" in out.css

    def test_scroll_right_emits_reverse_animation(self):
        cfg = TickerWidgetConfig(text="hi", mode="scroll", direction="right")
        out = TickerWidget().render_html(cfg, _cell(), "abc")
        assert "infinite reverse" in out.css

    def test_bounce_left_emits_alternate(self):
        cfg = TickerWidgetConfig(text="hi", mode="bounce", direction="left")
        out = TickerWidget().render_html(cfg, _cell(), "abc")
        assert "infinite alternate" in out.css
        # Must not be alternate-reverse here
        assert "alternate-reverse" not in out.css

    def test_bounce_right_emits_alternate_reverse(self):
        cfg = TickerWidgetConfig(text="hi", mode="bounce", direction="right")
        out = TickerWidget().render_html(cfg, _cell(), "abc")
        assert "alternate-reverse" in out.css

    def test_bounce_keeps_keyframe_name_scoped(self):
        cfg = TickerWidgetConfig(text="hi", mode="bounce")
        out = TickerWidget().render_html(cfg, _cell(), "xyz")
        assert "ticker-scroll-xyz" in out.css


# ────────────────────────────────────────────────────────────────────
# Cross-contract: variants build through the bundle pipeline
# ────────────────────────────────────────────────────────────────────


class TestVariantsBundleEndToEnd:
    """Variants must build cleanly through build_bundle with no
    contract changes.  These are the proof tests for the "extensibility
    without core changes" claim."""

    def _layout_with(self, widget_type: str, config: dict) -> Layout:
        return Layout(
            schema_version=1,
            widgets=[
                WidgetInstance(
                    id=uuid.uuid4(),
                    type=widget_type,
                    config=config,
                    cell=Cell(row=1, col=1, rowspan=2, colspan=4),
                )
            ],
        )

    def test_bold_italic_display_font_builds(self):
        layout = self._layout_with(
            "text",
            {
                "text": "Big bold thing",
                "color": "#ff0000",
                "font_size_px": 96,
                "font_family": "display",
                "bold": True,
                "italic": True,
            },
        )
        bundle = build_bundle(layout, get_registry())
        assert "Big bold thing" in bundle.html_bytes.decode("utf-8")
        assert "font-weight: 700" in bundle.html_bytes.decode("utf-8")
        assert "font-style: italic" in bundle.html_bytes.decode("utf-8")
        assert "Impact" in bundle.html_bytes.decode("utf-8")

    def test_handwritten_font_builds(self):
        layout = self._layout_with(
            "text",
            {
                "text": "casual",
                "color": "#000000",
                "font_size_px": 32,
                "font_family": "handwritten",
                "bold": False,
                "italic": False,
            },
        )
        bundle = build_bundle(layout, get_registry())
        assert "cursive" in bundle.html_bytes.decode("utf-8")

    def test_bounce_ticker_builds(self):
        layout = self._layout_with(
            "ticker",
            {
                "text": "boing",
                "speed_px_per_sec": 150,
                "direction": "right",
                "mode": "bounce",
                "color": "#ffffff",
                "background": "#000000",
                "font_family": "sans",
                "font_size_px": 32,
                "gap_px": 50,
            },
        )
        bundle = build_bundle(layout, get_registry())
        assert "alternate-reverse" in bundle.html_bytes.decode("utf-8")
        assert "boing" in bundle.html_bytes.decode("utf-8")


# ────────────────────────────────────────────────────────────────────
# Extensibility contract guardrail
# ────────────────────────────────────────────────────────────────────


class TestExtensibilityContractGuardrail:
    """Pins the Widget plugin contract surface.

    If any of these break, a future PR has changed the contract.  Either:
    (a) revert the change — variants must not require core changes, or
    (b) update this test deliberately, and document the new contract.

    Either way the change is now visible in code review, which is the
    whole point of a guardrail.
    """

    def test_widget_render_html_signature_pinned(self):
        """``render_html(self, config, cell, instance_id, ctx=None)``."""
        sig = inspect.signature(Widget.render_html)
        params = list(sig.parameters.keys())
        assert params == ["self", "config", "cell", "instance_id", "ctx"], (
            f"Widget.render_html signature drifted: {params!r}. "
            "Variant PRs must not require contract changes — revert "
            "or explicitly update this guardrail."
        )
        ctx_param = sig.parameters["ctx"]
        assert ctx_param.default is None, (
            "ctx must default to None so trivial widgets / tests can omit it"
        )

    def test_widget_declared_asset_ids_signature_pinned(self):
        sig = inspect.signature(Widget.declared_asset_ids)
        params = list(sig.parameters.keys())
        assert params == ["self", "config"], (
            f"Widget.declared_asset_ids signature drifted: {params!r}"
        )

    def test_widget_required_class_attrs(self):
        # The contract requires these attributes exist on Widget itself
        # (subclasses set them; the names are pinned).
        for attr in (
            "slug",
            "display_name",
            "icon",
            "ConfigSchema",
            "config_version",
        ):
            assert attr in Widget.__annotations__ or hasattr(Widget, attr), (
                f"Widget contract attribute {attr!r} missing"
            )

    def test_widget_optional_methods_present(self):
        for method_name in (
            "editor_template",
            "default_config",
            "migrate_config",
            "validate_semantic",
            "declared_asset_ids",
        ):
            assert callable(getattr(Widget, method_name)), (
                f"Widget.{method_name} missing — contract regression"
            )

    def test_bundle_context_fields_pinned(self):
        assert is_dataclass(BundleContext)
        names = {f.name for f in fields(BundleContext)}
        assert names == {
            "asset_bytes",
            "asset_mimes",
            "sibling_asset_urls",
            "cms_base_url",
            "slideshow_plans",
        }, (
            f"BundleContext fields drifted: {names!r}. Adding fields "
            "may be OK — explicitly update this pin and the contract docs."
        )

    def test_widget_render_fields_pinned(self):
        assert is_dataclass(WidgetRender)
        names = {f.name for f in fields(WidgetRender)}
        assert names == {
            "html",
            "css",
            "js",
            "init_js",
            "static_assets",
            "referenced_asset_ids",
        }, f"WidgetRender fields drifted: {names!r}"

    def test_asset_loader_alias_callable_shape(self):
        # AssetLoader is a type alias; importing it from bundle module
        # is part of the contract surface.
        assert AssetLoader is not None

    def test_missing_asset_loader_error_is_bundle_validation_error(self):
        assert issubclass(MissingAssetLoaderError, BundleValidationError)

    def test_widget_registry_public_api_pinned(self):
        for method in ("register", "get", "has", "slugs", "all"):
            assert callable(getattr(WidgetRegistry, method)), (
                f"WidgetRegistry.{method} missing"
            )

    def test_round2_variants_added_no_core_methods(self):
        """The bold/italic/display/bounce additions must not have
        introduced new Widget base-class methods or new BundleContext
        fields — that's the whole "no core changes" proof."""
        widget_methods = {
            n for n, v in inspect.getmembers(Widget, predicate=inspect.isfunction)
        }
        # Pin the exact method set on the base class.  Adding a method
        # here is a contract change and must be a deliberate PR.
        expected = {
            "render_html",
            "editor_template",
            "default_config",
            "migrate_config",
            "declared_asset_ids",
            "validate_semantic",
        }
        assert widget_methods == expected, (
            f"Widget base methods drifted from {expected!r} to {widget_methods!r}. "
            "If this was deliberate (e.g. you added a new contract hook), "
            "update this guardrail explicitly."
        )
