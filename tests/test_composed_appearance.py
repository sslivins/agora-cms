"""Tests for the per-widget Appearance ("frame") styling system.

Option B: every widget can carry an optional ``WidgetFrame`` that the
bundle builder turns into inline CSS on the shared ``.cw-cell`` wrapper
(corner radius, border, opacity, inset/padding, optional background
fill).  Covered here:

- ``WidgetFrame`` Pydantic range/shape validation.
- ``WidgetInstance.frame`` is optional and backward compatible.
- The bundle emits only non-default declarations.
- A ``frame=None`` (or all-default) widget produces a bundle that is
  byte-identical to a pre-appearance bundle (so existing slide hashes
  never churn and devices don't needlessly re-cache).
"""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

# Import to trigger widget auto-registration into the global registry.
import cms.composed.widgets  # noqa: F401
from cms.composed.bundle import build_bundle
from cms.composed.schema import (
    Cell,
    Layout,
    WidgetFrame,
    WidgetInstance,
)

_WID = uuid.UUID("11111111-1111-1111-1111-111111111111")


def _text_layout(frame: WidgetFrame | None = None) -> Layout:
    return Layout(
        widgets=[
            WidgetInstance(
                id=_WID,
                type="text",
                cell=Cell(row=1, col=1, rowspan=2, colspan=3),
                config={"text": "Hello", "color": "#ff0000"},
                frame=frame,
            ),
        ],
    )


# ── Schema validation ────────────────────────────────────────────────


class TestWidgetFrameSchema:
    def test_defaults_are_all_neutral(self):
        f = WidgetFrame()
        assert f.corner_radius == 0
        assert f.border_width == 0
        assert f.border_color == "#000000"
        assert f.opacity == 1.0
        assert f.inset == 0
        assert f.background is None

    def test_frame_is_optional_on_widget_instance(self):
        inst = WidgetInstance(
            id=_WID,
            type="text",
            cell=Cell(row=1, col=1),
            config={"text": "x"},
        )
        assert inst.frame is None

    @pytest.mark.parametrize("value", [-1, 501])
    def test_corner_radius_out_of_range_rejected(self, value):
        with pytest.raises(ValidationError):
            WidgetFrame(corner_radius=value)

    @pytest.mark.parametrize("value", [-1, 51])
    def test_border_width_out_of_range_rejected(self, value):
        with pytest.raises(ValidationError):
            WidgetFrame(border_width=value)

    @pytest.mark.parametrize("value", [-1, 501])
    def test_inset_out_of_range_rejected(self, value):
        with pytest.raises(ValidationError):
            WidgetFrame(inset=value)

    @pytest.mark.parametrize("value", [-0.01, 1.01])
    def test_opacity_out_of_range_rejected(self, value):
        with pytest.raises(ValidationError):
            WidgetFrame(opacity=value)

    @pytest.mark.parametrize("color", ["red", "#fff", "#1234567", "fff000"])
    def test_border_color_must_be_six_hex(self, color):
        with pytest.raises(ValidationError):
            WidgetFrame(border_color=color)

    @pytest.mark.parametrize("color", ["red", "#abc", "rgb(0,0,0)"])
    def test_background_must_be_six_hex_when_set(self, color):
        with pytest.raises(ValidationError):
            WidgetFrame(background=color)

    def test_background_none_allowed(self):
        assert WidgetFrame(background=None).background is None

    def test_extra_keys_forbidden(self):
        with pytest.raises(ValidationError):
            WidgetFrame(bogus=1)


# ── Bundle CSS emission ──────────────────────────────────────────────


class TestFrameBundleEmission:
    def _cell_style(self, html: str) -> str:
        marker = 'data-widget-instance="11111111-1111-1111-1111-111111111111"'
        idx = html.index(marker)
        start = html.index('style="', idx) + len('style="')
        end = html.index('"', start)
        return html[start:end]

    def test_full_frame_emits_all_decls(self):
        frame = WidgetFrame(
            corner_radius=24,
            border_width=4,
            border_color="#112233",
            opacity=0.5,
            inset=12,
            background="#445566",
        )
        html = build_bundle(_text_layout(frame)).html_bytes.decode("utf-8")
        style = self._cell_style(html)

        assert "box-sizing: border-box;" in style
        assert "padding: 12px;" in style
        assert "background: #445566;" in style
        assert "border: 4px solid #112233;" in style
        assert "border-radius: 24px;" in style
        assert "opacity: 0.5;" in style

    def test_partial_frame_emits_only_set_decls(self):
        frame = WidgetFrame(corner_radius=16)
        html = build_bundle(_text_layout(frame)).html_bytes.decode("utf-8")
        style = self._cell_style(html)

        assert "border-radius: 16px;" in style
        assert "box-sizing: border-box;" in style
        # Untouched fields must NOT emit declarations.
        assert "padding:" not in style
        assert "border:" not in style
        assert "opacity:" not in style
        assert "background:" not in style

    def test_default_frame_emits_no_decls(self):
        # A frame object whose every field is default must behave exactly
        # like frame=None — no box-sizing, no decls.
        html = build_bundle(_text_layout(WidgetFrame())).html_bytes.decode("utf-8")
        style = self._cell_style(html)
        assert "box-sizing" not in style
        assert "border-radius" not in style


# ── Backward compatibility / hash stability ──────────────────────────


class TestFrameBackwardCompat:
    def test_frame_none_is_byte_identical_to_default_frame(self):
        none_bundle = build_bundle(_text_layout(None))
        default_bundle = build_bundle(_text_layout(WidgetFrame()))
        assert none_bundle.html_bytes == default_bundle.html_bytes
        assert none_bundle.sha256_hex == default_bundle.sha256_hex

    def test_framed_bundle_differs_from_unframed(self):
        plain = build_bundle(_text_layout(None))
        framed = build_bundle(_text_layout(WidgetFrame(corner_radius=10)))
        assert plain.html_bytes != framed.html_bytes
        assert plain.sha256_hex != framed.sha256_hex


# ── Inset + corner-radius inner wrapper (rounded inset content) ──────


class TestFrameInnerWrap:
    """corner_radius + inset must round the *inset content*, not just clip
    at the outer padding box (which leaves inset images square-cornered).
    """

    def _inner_div(self, html: str) -> str | None:
        idx = html.find('class="cw-cell-inner"')
        if idx == -1:
            return None
        start = html.index('style="', idx) + len('style="')
        end = html.index('"', start)
        return html[start:end]

    def test_radius_with_inset_emits_inner_wrapper(self):
        frame = WidgetFrame(corner_radius=40, inset=10)
        html = build_bundle(_text_layout(frame)).html_bytes.decode("utf-8")
        inner = self._inner_div(html)
        assert inner is not None
        # Reduced concentric radius: 40 - 10 inset - 0 border = 30.
        assert "border-radius: 30px;" in inner
        assert "overflow: hidden;" in inner

    def test_inner_radius_also_reduced_by_border(self):
        frame = WidgetFrame(corner_radius=40, inset=10, border_width=5)
        html = build_bundle(_text_layout(frame)).html_bytes.decode("utf-8")
        inner = self._inner_div(html)
        assert inner is not None
        # 40 - 10 inset - 5 border = 25.
        assert "border-radius: 25px;" in inner

    def test_inner_radius_clamped_at_zero(self):
        # Inset larger than the radius can't make a negative radius.
        frame = WidgetFrame(corner_radius=10, inset=40)
        html = build_bundle(_text_layout(frame)).html_bytes.decode("utf-8")
        inner = self._inner_div(html)
        assert inner is not None
        assert "border-radius: 0px;" in inner

    def test_radius_without_inset_has_no_inner_wrapper(self):
        # No inset → outer clip rounds the content correctly; no wrapper,
        # so these bundles stay byte-identical to the pre-fix output.
        html = build_bundle(
            _text_layout(WidgetFrame(corner_radius=24))
        ).html_bytes.decode("utf-8")
        assert "cw-cell-inner" not in html

    def test_inset_without_radius_has_no_inner_wrapper(self):
        html = build_bundle(
            _text_layout(WidgetFrame(inset=20))
        ).html_bytes.decode("utf-8")
        assert "cw-cell-inner" not in html

    def test_no_frame_has_no_inner_wrapper(self):
        html = build_bundle(_text_layout(None)).html_bytes.decode("utf-8")
        assert "cw-cell-inner" not in html
