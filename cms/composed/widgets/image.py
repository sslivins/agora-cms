"""Image widget — embeds a single Asset (type=IMAGE) into the bundle.

Pulls the asset's bytes from the per-build :class:`BundleContext`
that the bundle builder pre-fetches via the ``asset_loader`` callback
(see :mod:`cms.composed.bundle`).  The image is inlined as a base64
``data:`` URI inside an ``<img>`` tag so the rendered bundle stays
self-contained (no external network fetches at playback time).

Sizing model: the widget always fills its grid cell.  ``object_fit``
controls how the source image is scaled inside that cell:

* ``cover``  — fill cell, crop overflow (default; matches CMS asset
  thumbnail behaviour)
* ``contain`` — fit fully inside cell, letterbox the gaps
* ``fill``   — stretch to cell ignoring aspect ratio

Future work (Phase 2+): server-side resize-on-publish to avoid
shipping a 4K photo through a data URI for a 200px cell.  For 1B,
we just warn on bundles where any single image exceeds 2 MiB.
"""

from __future__ import annotations

import base64
import logging
import uuid
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from cms.composed.registry import BundleContext, Widget, WidgetRender
from cms.composed.schema import Cell


log = logging.getLogger(__name__)


# Soft warning threshold for inlined image size.  Above this the
# widget logs a warning so we have a breadcrumb in the publish logs
# when a bundle explodes in size.  Not a hard cap — the editor will
# surface this in Phase 2.
_LARGE_IMAGE_WARN_BYTES: int = 2 * 1024 * 1024


class ImageWidgetConfig(BaseModel):
    """User-editable config for :class:`ImageWidget`."""

    model_config = ConfigDict(extra="forbid")

    asset_id: uuid.UUID
    # Mirrors the CSS ``object-fit`` allowlist we actually support.
    # The CSS ``object-position: center`` default is fine for v1.
    object_fit: Literal["cover", "contain", "fill"] = "cover"
    # Optional human-readable alt text for accessibility / debug.
    # Always HTML-escaped before emission.
    alt: str = Field(default="", max_length=512)


class ImageWidget(Widget):
    """Single image asset, inlined as a data URI."""

    slug: ClassVar[str] = "image"
    display_name: ClassVar[str] = "Image"
    icon: ClassVar[str] = "🖼"
    ConfigSchema: ClassVar[type[BaseModel]] = ImageWidgetConfig
    config_version: ClassVar[int] = 1

    def default_config(self) -> dict:
        # The editor will replace ``asset_id`` immediately upon drop;
        # the all-zero UUID is just a placeholder that validates
        # shape-wise but will fail :func:`validate_layout` (because the
        # referenced asset doesn't exist).  This is intentional — it
        # forces the user to pick an asset before save.
        return {
            "asset_id": "00000000-0000-0000-0000-000000000000",
            "object_fit": "cover",
            "alt": "",
        }

    def editor_template(self) -> str:
        return "composed/widgets/image.html"

    def declared_asset_ids(self, config: BaseModel) -> list[uuid.UUID]:
        assert isinstance(config, ImageWidgetConfig)
        return [config.asset_id]

    def render_html(
        self,
        config: BaseModel,
        cell: Cell,
        instance_id: str,
        ctx: BundleContext | None = None,
    ) -> WidgetRender:
        assert isinstance(config, ImageWidgetConfig), (
            "ImageWidget.render_html expects an ImageWidgetConfig instance"
        )
        if ctx is None:
            # Bundle builder always passes one; the only way to hit this
            # is to call render_html directly without going through
            # build_bundle (e.g., a unit test that forgot the ctx arg).
            raise RuntimeError(
                "ImageWidget.render_html requires a BundleContext "
                "with asset bytes pre-loaded"
            )

        asset_id = config.asset_id
        try:
            blob = ctx.asset_bytes[asset_id]
            mime = ctx.asset_mimes[asset_id]
        except KeyError:
            # The bundle builder enforces declare-before-reference; if
            # we end up here it means the loader silently returned
            # nothing or a widget bypassed declared_asset_ids().  Bail
            # loudly rather than emit a broken data: URI.
            raise RuntimeError(
                f"ImageWidget instance {instance_id}: asset {asset_id} "
                "missing from BundleContext"
            ) from None

        if len(blob) > _LARGE_IMAGE_WARN_BYTES:
            log.warning(
                "composed image widget %s: asset %s is %d bytes (>%d) — "
                "inlined bundle will be large",
                instance_id,
                asset_id,
                len(blob),
                _LARGE_IMAGE_WARN_BYTES,
            )

        css_class = f"cw-image-{instance_id}"
        b64 = base64.b64encode(blob).decode("ascii")
        data_uri = f"data:{mime};base64,{b64}"

        # Use ``html.escape`` via Pydantic-validated alt + the fact
        # that ``data_uri`` is hex-only base64 — no special chars to
        # escape in the attribute.  alt may contain user input though.
        import html as _html

        alt_escaped = _html.escape(config.alt)

        html_out = (
            f'<img class="{css_class}" '
            f'src="{data_uri}" '
            f'alt="{alt_escaped}" '
            f'draggable="false" />'
        )

        css_out = (
            f".{css_class} {{\n"
            f"  width: 100%;\n"
            f"  height: 100%;\n"
            f"  display: block;\n"
            f"  object-fit: {config.object_fit};\n"
            f"  object-position: center;\n"
            f"  user-select: none;\n"
            f"}}"
        )

        return WidgetRender(
            html=html_out,
            css=css_out,
            referenced_asset_ids=[asset_id],
        )
