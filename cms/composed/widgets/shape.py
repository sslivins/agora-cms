"""Shape widget — pure-CSS rectangle, ellipse, or line/divider.

The simplest *decorative* widget: it draws a single geometric primitive
filling its cell.  No JS, no asset dependencies, no external references —
everything is emitted as instance-scoped CSS, so a given config always
produces byte-identical output (bundle hash stability).

Three shapes:

* ``rectangle`` — a filled box.  ``corner_radius`` rounds its corners;
  ``border_width`` / ``border_color`` draw an outline.  Combine a high
  ``corner_radius`` with no fill (transparent) + a border to get a pill
  outline, or use it as a solid colour-block / matte panel behind other
  widgets.
* ``ellipse`` — same as rectangle but ``border-radius: 50%`` (a circle in
  a square cell, an oval otherwise).  ``corner_radius`` is ignored.
* ``line`` — a divider rule.  ``orientation`` picks horizontal/vertical;
  ``thickness`` sets its weight in design px; the line is centred in the
  cell and uses ``fill`` as its colour.  ``corner_radius`` rounds the
  line's end-caps.

Opacity / inset / a wrapper background are intentionally **not** config
fields here — the shared per-widget "Appearance" frame (the ``.cw-cell``
wrapper) already provides them for every widget.  Keeping them out avoids
two competing opacity knobs.

Instance scoping: the wrapper ``<div>`` class is suffixed with the widget
instance UUID so two shape widgets in the same bundle never collide.
"""

from __future__ import annotations

from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell

_HEX6 = r"^#[0-9a-fA-F]{6}$"


class ShapeWidgetConfig(BaseModel):
    """User-editable config for :class:`ShapeWidget`."""

    model_config = ConfigDict(extra="forbid")

    shape: Literal["rectangle", "ellipse", "line"] = "rectangle"
    # ``fill`` doubles as the line colour for ``shape == "line"``.
    fill: str = Field(default="#3b82f6", pattern=_HEX6)
    # Outline for rectangle/ellipse. Ignored for line.
    border_width: int = Field(default=0, ge=0, le=100)
    border_color: str = Field(default="#000000", pattern=_HEX6)
    # Rounds rectangle corners (and line end-caps). Ignored for ellipse.
    corner_radius: int = Field(default=0, ge=0, le=500)
    # Line-only geometry.
    thickness: int = Field(default=8, ge=1, le=500)
    orientation: Literal["horizontal", "vertical"] = "horizontal"


class ShapeWidget(Widget):
    """Pure-CSS geometric primitive (rectangle / ellipse / line)."""

    slug: ClassVar[str] = "shape"
    display_name: ClassVar[str] = "Shape"
    icon: ClassVar[str] = "⬛"
    ConfigSchema: ClassVar[type[BaseModel]] = ShapeWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        return {
            "shape": "rectangle",
            "fill": "#3b82f6",
            "border_width": 0,
            "border_color": "#000000",
            "corner_radius": 0,
            "thickness": 8,
            "orientation": "horizontal",
        }

    def editor_template(self) -> str:
        return "composed/widgets/shape.html"

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        # Shape has no asset deps; ctx/cell are ignored.
        del ctx, cell
        assert isinstance(config, ShapeWidgetConfig), (
            "ShapeWidget.render_html expects a ShapeWidgetConfig instance"
        )

        css_class = f"cw-shape-{instance_id}"

        if config.shape == "line":
            css_out, html_out = self._render_line(config, css_class)
        else:
            css_out, html_out = self._render_box(config, css_class)

        return WidgetRender(html=html_out, css=css_out)

    def _render_box(
        self, config: ShapeWidgetConfig, css_class: str
    ) -> tuple[str, str]:
        """Rectangle or ellipse — a single filled, optionally bordered box."""
        decls = [
            "width: 100%;",
            "height: 100%;",
            "box-sizing: border-box;",
            f"background: {config.fill};",
        ]
        if config.shape == "ellipse":
            decls.append("border-radius: 50%;")
        elif config.corner_radius > 0:
            decls.append(f"border-radius: {config.corner_radius}px;")
        if config.border_width > 0:
            decls.append(
                f"border: {config.border_width}px solid {config.border_color};"
            )
        body = "\n".join(f"  {d}" for d in decls)
        css_out = f".{css_class} {{\n{body}\n}}"
        html_out = f'<div class="{css_class}"></div>'
        return css_out, html_out

    def _render_line(
        self, config: ShapeWidgetConfig, css_class: str
    ) -> tuple[str, str]:
        """Centered divider rule, horizontal or vertical."""
        if config.orientation == "vertical":
            size_decls = (
                f"width: {config.thickness}px;\n"
                f"  height: 100%;"
            )
        else:
            size_decls = (
                f"width: 100%;\n"
                f"  height: {config.thickness}px;"
            )
        radius = (
            f"\n  border-radius: {config.corner_radius}px;"
            if config.corner_radius > 0
            else ""
        )
        # Wrapper centers the rule both ways so it sits in the middle of
        # the cell regardless of the cell's aspect ratio.
        css_out = (
            f".{css_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: flex;\n"
            f"  align-items: center;\n"
            f"  justify-content: center;\n"
            f"}}\n"
            f".{css_class}-rule {{\n"
            f"  {size_decls}\n"
            f"  background: {config.fill};{radius}\n"
            f"}}"
        )
        html_out = (
            f'<div class="{css_class}">'
            f'<div class="{css_class}-rule"></div>'
            f"</div>"
        )
        return css_out, html_out
