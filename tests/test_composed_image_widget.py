"""Tests for cms.composed.widgets.image.ImageWidget.

Covers config validation, render output, instance scoping, and the
declared/referenced asset contract.  The widget reads bytes from
:class:`BundleContext`; tests construct one synthetically rather than
going through ``build_bundle`` so failures point at the widget
implementation itself, not the pipeline.
"""

from __future__ import annotations

import base64
import logging
import uuid

import pytest
from pydantic import ValidationError

# Side-effect import: triggers global registry population
import cms.composed.widgets  # noqa: F401
from cms.composed.registry import BundleContext, get_registry
from cms.composed.schema import Cell
from cms.composed.widgets.image import (
    ImageWidget,
    ImageWidgetConfig,
)


def _cell() -> Cell:
    return Cell(row=1, col=1, rowspan=2, colspan=3)


def _ctx_for(aid: uuid.UUID, blob: bytes, mime: str = "image/png") -> BundleContext:
    return BundleContext(
        asset_bytes={aid: blob},
        asset_mimes={aid: mime},
    )


# A trivial 1x1 transparent PNG — enough to exercise the data-URI path.
_TINY_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c489"
    "0000000d49444154789c63600100000005000168cb6e6d0000000049454e44ae426082"
)


class TestImageWidgetConfig:
    def test_minimum_valid_config(self):
        aid = uuid.uuid4()
        c = ImageWidgetConfig(asset_id=aid)
        assert c.asset_id == aid
        assert c.object_fit == "cover"
        assert c.alt == ""

    def test_asset_id_required(self):
        with pytest.raises(ValidationError):
            ImageWidgetConfig()  # type: ignore[call-arg]

    def test_object_fit_allowlist(self):
        for ok in ("cover", "contain", "fill"):
            ImageWidgetConfig(asset_id=uuid.uuid4(), object_fit=ok)
        with pytest.raises(ValidationError):
            ImageWidgetConfig(asset_id=uuid.uuid4(), object_fit="scale-down")

    def test_alt_max_length(self):
        with pytest.raises(ValidationError):
            ImageWidgetConfig(asset_id=uuid.uuid4(), alt="x" * 513)

    def test_extra_field_rejected(self):
        with pytest.raises(ValidationError):
            ImageWidgetConfig(asset_id=uuid.uuid4(), unknown="nope")  # type: ignore[call-arg]


class TestImageWidgetRegistry:
    def test_registered(self):
        w = get_registry().get("image")
        assert isinstance(w, ImageWidget)
        assert w.slug == "image"
        assert w.config_version == 1


class TestImageWidgetDeclaredAssetIds:
    def test_returns_single_asset_id(self):
        aid = uuid.uuid4()
        w = ImageWidget()
        cfg = ImageWidgetConfig(asset_id=aid)
        assert w.declared_asset_ids(cfg) == [aid]


class TestImageWidgetRender:
    def test_requires_ctx(self):
        # Calling without a BundleContext is a programmer bug — it
        # would silently emit a broken data: URI without this guard.
        w = ImageWidget()
        cfg = ImageWidgetConfig(asset_id=uuid.uuid4())
        with pytest.raises(RuntimeError, match="BundleContext"):
            w.render_html(cfg, _cell(), "id-1", ctx=None)

    def test_missing_asset_in_ctx_raises(self):
        w = ImageWidget()
        aid = uuid.uuid4()
        cfg = ImageWidgetConfig(asset_id=aid)
        # Pass an empty ctx — widget should bail loudly rather than
        # render an <img> with no src.
        with pytest.raises(RuntimeError, match="missing from BundleContext"):
            w.render_html(cfg, _cell(), "id-1", ctx=BundleContext())

    def test_renders_data_uri_with_b64_content(self):
        w = ImageWidget()
        aid = uuid.uuid4()
        cfg = ImageWidgetConfig(asset_id=aid, alt="cat")
        ctx = _ctx_for(aid, _TINY_PNG, "image/png")

        r = w.render_html(cfg, _cell(), "abc", ctx=ctx)

        b64 = base64.b64encode(_TINY_PNG).decode("ascii")
        assert f'src="data:image/png;base64,{b64}"' in r.html
        assert 'alt="cat"' in r.html

    def test_referenced_asset_ids_match_declared(self):
        w = ImageWidget()
        aid = uuid.uuid4()
        cfg = ImageWidgetConfig(asset_id=aid)
        ctx = _ctx_for(aid, _TINY_PNG)
        r = w.render_html(cfg, _cell(), "abc", ctx=ctx)
        assert r.referenced_asset_ids == [aid]

    def test_html_and_css_are_instance_scoped(self):
        w = ImageWidget()
        aid = uuid.uuid4()
        cfg = ImageWidgetConfig(asset_id=aid)
        ctx = _ctx_for(aid, _TINY_PNG)

        r1 = w.render_html(cfg, _cell(), "inst-A", ctx=ctx)
        r2 = w.render_html(cfg, _cell(), "inst-B", ctx=ctx)

        assert "cw-image-inst-A" in r1.html
        assert "cw-image-inst-A" in r1.css
        assert "cw-image-inst-B" in r2.html
        assert "cw-image-inst-B" in r2.css
        # No cross-contamination
        assert "cw-image-inst-B" not in r1.html
        assert "cw-image-inst-A" not in r2.html

    def test_alt_text_is_html_escaped(self):
        w = ImageWidget()
        aid = uuid.uuid4()
        cfg = ImageWidgetConfig(asset_id=aid, alt='</alt><script>bad()</script>')
        ctx = _ctx_for(aid, _TINY_PNG)

        r = w.render_html(cfg, _cell(), "abc", ctx=ctx)

        # The literal payload must NOT escape the alt attribute.
        assert "<script>" not in r.html
        assert "&lt;script&gt;" in r.html
        assert "&lt;/alt&gt;" in r.html

    def test_object_fit_propagates_to_css(self):
        w = ImageWidget()
        aid = uuid.uuid4()
        ctx = _ctx_for(aid, _TINY_PNG)
        for fit in ("cover", "contain", "fill"):
            cfg = ImageWidgetConfig(asset_id=aid, object_fit=fit)  # type: ignore[arg-type]
            r = w.render_html(cfg, _cell(), "abc", ctx=ctx)
            assert f"object-fit: {fit}" in r.css

    def test_large_image_logs_warning(self, caplog):
        w = ImageWidget()
        aid = uuid.uuid4()
        cfg = ImageWidgetConfig(asset_id=aid)
        big = b"\x00" * (3 * 1024 * 1024)  # 3 MiB > 2 MiB threshold
        ctx = _ctx_for(aid, big, "image/png")

        with caplog.at_level(logging.WARNING, logger="cms.composed.widgets.image"):
            w.render_html(cfg, _cell(), "big", ctx=ctx)

        assert any("inlined bundle will be large" in rec.message for rec in caplog.records)
