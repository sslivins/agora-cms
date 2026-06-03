"""Composed-slide bundle builder.

Renders a :class:`~cms.composed.schema.Layout` into a single
self-contained HTML document with all CSS / JS / fonts / images
inlined as data URIs or embedded ``<style>`` / ``<script>`` blocks.

The resulting bundle has **zero external src= / href= references**
to non-``data:`` URLs.  This is the file we ship to devices: the
existing per-asset cache layer downloads it once and replays it
offline-tolerantly.

Determinism: building the same layout twice yields byte-identical
output (so :func:`hashlib.sha256` is a stable cache key).  We achieve
this by iterating widgets in their layout order and de-duplicating
static assets by content hash *in first-appearance order*.

See ``plan.md`` for the delivery / artifact contract.
"""

from __future__ import annotations

import base64
import hashlib
import html
import uuid
from dataclasses import dataclass

from cms.composed.registry import (
    Widget,
    WidgetRegistry,
    WidgetRender,
    WidgetStaticAsset,
    get_registry,
)
from cms.composed.schema import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    GRID_COLS,
    GRID_ROWS,
    Layout,
    WidgetInstance,
)
from cms.composed.validate import ValidationError, validate_layout


class BundleValidationError(Exception):
    """Raised when :func:`build_bundle` is called on an invalid layout.

    Carries the structured validator errors so callers (the publish
    service, the preview endpoint) can surface them to the user.
    """

    def __init__(self, errors: list[ValidationError]) -> None:
        self.errors = errors
        msgs = "; ".join(f"[{e.code}] {e.message}" for e in errors)
        super().__init__(f"layout has {len(errors)} validation error(s): {msgs}")


@dataclass
class BuiltBundle:
    """Result of :func:`build_bundle`."""

    html_bytes: bytes
    sha256_hex: str
    source_asset_ids: list[uuid.UUID]


# ── Helpers ──────────────────────────────────────────────────────────


def _instance_id_str(inst: WidgetInstance) -> str:
    # Keep the on-the-wire UUID format the editor sees; widgets scope
    # CSS classes by this string verbatim.
    return str(inst.id)


def _grid_area_style(inst: WidgetInstance) -> str:
    cell = inst.cell
    # CSS grid is 1-indexed; row-end / column-end are exclusive (the
    # "span N" syntax keeps the intent obvious in the emitted markup).
    return (
        f"grid-row: {cell.row} / span {cell.rowspan}; "
        f"grid-column: {cell.col} / span {cell.colspan};"
    )


def _data_uri(asset: WidgetStaticAsset) -> str:
    if isinstance(asset.content, bytes):
        payload = base64.b64encode(asset.content).decode("ascii")
        return f"data:{asset.mime};base64,{payload}"
    # Text content — still base64 it so we don't have to URL-escape
    # arbitrary characters and so binary/text behave the same.
    payload = base64.b64encode(asset.content.encode("utf-8")).decode("ascii")
    return f"data:{asset.mime};base64,{payload}"


def _hash_text(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _resolve_widget(reg: WidgetRegistry, inst: WidgetInstance) -> Widget:
    w = reg.get(inst.type)
    if w is None:
        # Defensive — validate_layout should have caught this already.
        raise BundleValidationError(
            [
                ValidationError(
                    code="unknown_widget_type",
                    message=f"widget {inst.id}: unknown type {inst.type!r}",
                    widget_id=str(inst.id),
                )
            ]
        )
    return w


# ── Main entrypoint ──────────────────────────────────────────────────


def build_bundle(
    layout: Layout,
    registry: WidgetRegistry | None = None,
) -> BuiltBundle:
    """Render ``layout`` to a self-contained HTML bundle.

    Raises :class:`BundleValidationError` if the layout fails
    :func:`~cms.composed.validate.validate_layout`.  The check is
    redundant with the editor's save-time validation but kept as
    defense-in-depth so we never embed a bad layout.
    """

    reg = registry if registry is not None else get_registry()

    # 1. Defensive validation.
    errors = validate_layout(layout, reg)
    if errors:
        raise BundleValidationError(errors)

    # 2. Render each widget, collecting css/js/static assets +
    #    accumulated referenced asset IDs for staleness tracking.
    rendered: list[tuple[WidgetInstance, WidgetRender]] = []
    source_asset_ids: list[uuid.UUID] = []
    seen_asset_ids: set[uuid.UUID] = set()

    for inst in layout.widgets:
        widget = _resolve_widget(reg, inst)
        # The validator already constructed + checked the config; re-do
        # it here so render_html gets a typed config object rather than
        # a raw dict.  Cheap (Pydantic is fast for small models).
        config = widget.ConfigSchema.model_validate(inst.config)
        render: WidgetRender = widget.render_html(
            config=config,
            cell=inst.cell,
            instance_id=_instance_id_str(inst),
        )
        rendered.append((inst, render))

        for aid in render.referenced_asset_ids:
            if aid not in seen_asset_ids:
                seen_asset_ids.add(aid)
                source_asset_ids.append(aid)

    # 3. De-dup CSS / JS / static assets by content hash, preserving
    #    first-appearance order for deterministic output.
    css_blocks: dict[str, str] = {}
    js_blocks: dict[str, str] = {}
    static_assets: dict[str, WidgetStaticAsset] = {}

    for _inst, render in rendered:
        if render.css.strip():
            css_blocks.setdefault(_hash_text(render.css), render.css)
        if render.js.strip():
            js_blocks.setdefault(_hash_text(render.js), render.js)
        for sa in render.static_assets:
            blob = (
                sa.content if isinstance(sa.content, bytes) else sa.content.encode("utf-8")
            )
            key = hashlib.sha256(blob).hexdigest() + ":" + sa.mime
            static_assets.setdefault(key, sa)

    # 4. Assemble the document.
    #    All CSS / JS is inlined.  Static assets are turned into data
    #    URIs when widgets reference them via their own CSS (e.g.
    #    @font-face url(...) — Phase 1B); here we keep the structural
    #    skeleton.  For Phase 1A only text widgets exist, so the
    #    static_assets dict is empty in practice.

    page_css_parts: list[str] = []
    page_css_parts.append(_base_css(layout))
    for _h, css in css_blocks.items():
        page_css_parts.append(css)
    # Bundle static assets that arrived as kind="css" inline too —
    # mirrors the per-widget css channel but allows widgets to ship
    # bigger / library CSS as a static asset for de-dup.
    for _k, sa in static_assets.items():
        if sa.kind == "css":
            text = sa.content if isinstance(sa.content, str) else sa.content.decode("utf-8")
            page_css_parts.append(text)

    page_js_parts: list[str] = []
    for _k, sa in static_assets.items():
        if sa.kind == "js":
            text = sa.content if isinstance(sa.content, str) else sa.content.decode("utf-8")
            page_js_parts.append(text)
    for _h, js in js_blocks.items():
        page_js_parts.append(js)

    # Per-instance init_js, wrapped in a function so each instance's
    # locals don't bleed into the next one.  Each block gets its own
    # ``instanceId`` parameter for convenience inside widget code.
    init_blocks: list[str] = []
    for inst, render in rendered:
        if render.init_js:
            wid = _instance_id_str(inst)
            init_blocks.append(
                "(function(instanceId){\n"
                + render.init_js
                + "\n}).call(null, " + _js_string_literal(wid) + ");"
            )

    init_script = ""
    if init_blocks:
        init_script = (
            "document.addEventListener('DOMContentLoaded', function(){\n"
            + "\n".join(init_blocks)
            + "\n});"
        )

    widget_html_blocks: list[str] = []
    for inst, render in rendered:
        wid = _instance_id_str(inst)
        widget_html_blocks.append(
            f'<div class="cw-cell" data-widget-instance="{html.escape(wid)}" '
            f'style="{_grid_area_style(inst)}">{render.html}</div>'
        )

    doc = _DOC_TEMPLATE.format(
        title="Composed Slide",
        style="\n".join(p for p in page_css_parts if p),
        body_grid_inner="\n".join(widget_html_blocks),
        script_body="\n".join(p for p in page_js_parts if p),
        init_script=init_script,
    )

    html_bytes = doc.encode("utf-8")
    sha = hashlib.sha256(html_bytes).hexdigest()
    return BuiltBundle(
        html_bytes=html_bytes,
        sha256_hex=sha,
        source_asset_ids=source_asset_ids,
    )


# ── Templates / boilerplate ──────────────────────────────────────────


def _base_css(layout: Layout) -> str:
    bg = layout.background.color
    return (
        "html,body{margin:0;padding:0;width:100%;height:100%;"
        "background:#000;overflow:hidden;}\n"
        ".cw-canvas{position:relative;width:100vw;height:100vh;"
        f"background:{bg};display:grid;"
        f"grid-template-columns:repeat({GRID_COLS}, 1fr);"
        f"grid-template-rows:repeat({GRID_ROWS}, 1fr);}}\n"
        ".cw-cell{position:relative;overflow:hidden;}"
    )


def _js_string_literal(s: str) -> str:
    """Serialise a Python string for safe embedding in a JS string.

    Stricter than :func:`json.dumps` for our needs — we want to keep
    HTML special chars escaped too so a stray ``</script>`` in a
    widget's instance id (it can't happen for UUIDs, but be defensive)
    doesn't terminate the script block.
    """

    out_chars: list[str] = ["'"]
    for ch in s:
        if ch == "'":
            out_chars.append("\\'")
        elif ch == "\\":
            out_chars.append("\\\\")
        elif ch == "<":
            out_chars.append("\\x3c")
        elif ch == ">":
            out_chars.append("\\x3e")
        elif ch == "\n":
            out_chars.append("\\n")
        elif ch == "\r":
            out_chars.append("\\r")
        else:
            out_chars.append(ch)
    out_chars.append("'")
    return "".join(out_chars)


# The document is small and intentionally readable so a human can
# open a saved bundle and reason about it without un-minifying.
_DOC_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width={CW},height={CH}">
<meta name="generator" content="agora-cms composed-bundle/1">
<title>{title}</title>
<style>
{style}
</style>
</head>
<body>
<div class="cw-canvas">
{body_grid_inner}
</div>
<script>
{script_body}
{init_script}
</script>
</body>
</html>
""".replace("{CW}", str(CANVAS_WIDTH)).replace("{CH}", str(CANVAS_HEIGHT))
