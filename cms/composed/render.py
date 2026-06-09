"""Shared composed-slide HTML renderer.

Builds the self-contained HTML document for a composed slide — all CSS
and JS inlined, every referenced image inlined as a base64 ``data:`` URI
and every referenced video inlined as a base64 ``data:`` URI — exactly
like the live preview endpoint.

This logic used to live inline in ``cms.routers.composed.preview_composed_slide``.
It was extracted here so that two callers can share it:

* the preview route (``GET /composed/{id}/preview``), which wraps it with
  per-asset ACL enforcement and CSP headers; and
* the **worker** thumbnail renderer, which runs the same HTML through a
  headless browser to produce a static JPEG snapshot. The worker is
  trusted infra and renders without ACL checks (it never serves the HTML
  to a user — only the resulting raster thumbnail, gated by the existing
  variant-preview endpoint).

Raising ``HTTPException`` from here is intentional: the preview route
relies on FastAPI translating these into the same 404/403/422 responses
it produced before the extraction. The worker wraps the call in
try/except and marks the variant failed on any error.

``fastapi`` is a CMS-only dependency and is **not** installed in the
worker image. So that the worker can import this module to render
thumbnails, ``HTTPException`` is imported lazily with a lightweight,
API-compatible fallback (same ``status_code`` / ``detail`` attributes)
when fastapi is absent. In the CMS the real ``fastapi.HTTPException`` is
used, so the preview route's response translation is unchanged.
"""

from __future__ import annotations

import asyncio
import base64
import mimetypes
import uuid
from dataclasses import dataclass
from typing import Awaitable, Callable

try:  # fastapi is a CMS-only dep; the worker image does not install it.
    from fastapi import HTTPException
except ModuleNotFoundError:  # pragma: no cover - exercised only in the worker image

    class HTTPException(Exception):  # type: ignore[no-redef]
        """Minimal stand-in for ``fastapi.HTTPException``.

        Mirrors the ``status_code`` / ``detail`` attributes so the raise
        sites below behave identically. The worker catches any exception
        from this module and marks the variant failed, so the concrete
        type does not matter there.
        """

        def __init__(self, status_code: int = 500, detail: object = None) -> None:
            self.status_code = status_code
            self.detail = detail
            super().__init__(detail)

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.composed.bundle import BundleValidationError, build_bundle
from cms.composed.registry import get_registry
from cms.composed.schema import Layout
from cms.composed.validate import validate_layout
from cms.models.asset import Asset, AssetType
from cms.models.composed_slide import ComposedSlide

# Widgets allowed to render a VIDEO asset. The ImageWidget only knows how
# to emit an <img> from inline bytes, so routing a video to it would raise
# at render time; restrict video to the media widget.
_VIDEO_CAPABLE_SLUGS = {"media"}

# Hard cap on a video inlined as a base64 data: URI. The locked-down
# preview CSP only permits ``media-src data:``, so preview/snapshot must
# inline the bytes. To avoid reading an arbitrarily large file into memory
# (and base64-bloating it by ~33%), refuse anything over this size.
_MAX_INLINE_VIDEO_BYTES = 32 * 1024 * 1024


@dataclass
class ComposedRender:
    """Result of rendering a composed slide to a self-contained HTML doc."""

    html_bytes: bytes
    has_weather: bool
    has_rss: bool = False


# Optional async hook called for the slide asset and every referenced
# asset before its bytes are inlined. The preview route passes a closure
# that enforces per-asset visibility; the worker passes ``None`` (trusted).
VerifyAsset = Callable[[uuid.UUID], Awaitable[None]]


def _read_capped(path, max_bytes: int) -> bytes | None:
    """Read up to ``max_bytes``; return None if the file is larger."""
    with path.open("rb") as fh:
        data = fh.read(max_bytes + 1)
    if len(data) > max_bytes:
        return None
    return data


async def _read_inline_asset(
    storage_dir, ref: Asset, *, max_bytes: int | None = None,
) -> bytes:
    """Read a referenced asset's bytes for inlining into a render.

    Enforces that the resolved path stays under ``storage_dir`` (defense
    in depth against a crafted filename) and, for videos, that the on-disk
    size — checked via ``stat`` *before* reading — is within ``max_bytes``.
    Raises 422 on any read / size problem.
    """
    base = storage_dir.resolve()
    path = (storage_dir / ref.filename).resolve()
    if not path.is_relative_to(base):
        raise HTTPException(
            status_code=422,
            detail=f"Asset {ref.id} has an invalid storage path",
        )
    try:
        if max_bytes is not None:
            actual = path.stat().st_size
            if actual > max_bytes:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Video asset {ref.id} is too large to render "
                        f"({actual} bytes; cap {max_bytes})"
                    ),
                )
            # Bounded read closes the stat->read TOCTOU window: if the file
            # grew/was swapped after the stat, refuse rather than inline
            # more than the cap.
            blob = await asyncio.to_thread(_read_capped, path, max_bytes)
            if blob is None:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Video asset {ref.id} is too large to render "
                        f"(exceeds cap {max_bytes})"
                    ),
                )
            return blob
        return await asyncio.to_thread(path.read_bytes)
    except HTTPException:
        raise
    except OSError as e:
        raise HTTPException(
            status_code=422,
            detail=f"Asset {ref.id} file missing or unreadable: {ref.filename}",
        ) from e


async def build_composed_html(
    db: AsyncSession,
    settings,
    asset_id: uuid.UUID,
    *,
    verify_asset: VerifyAsset | None = None,
) -> ComposedRender:
    """Render the composed slide bound to ``asset_id`` to self-contained HTML.

    Loads the saved layout, validates it, inlines every referenced image
    and video as a base64 ``data:`` URI, and returns the built HTML bytes
    along with a ``has_weather`` flag (so the preview route can widen its
    CSP for the one widget that makes a runtime network call).

    ``verify_asset``, when supplied, is awaited for the slide asset and for
    each referenced asset before its bytes are read; it should raise to
    deny access. The worker passes ``None`` (it is trusted and never
    exposes the HTML directly).

    Raises ``HTTPException`` 404 (missing / not composed / deleted), 403
    (via ``verify_asset``), or 422 (invalid layout, missing or wrong-typed
    referenced asset, oversized inlined video) — mirroring the preview
    endpoint's original behaviour.
    """
    asset = await db.get(Asset, asset_id)
    if (
        asset is None
        or asset.asset_type != AssetType.COMPOSED
        or asset.deleted_at is not None
    ):
        raise HTTPException(status_code=404, detail="Composed slide not found")

    if verify_asset is not None:
        await verify_asset(asset_id)

    cs_result = await db.execute(
        select(ComposedSlide).where(ComposedSlide.asset_id == asset_id),
    )
    composed = cs_result.scalar_one_or_none()
    if composed is None:
        raise HTTPException(status_code=404, detail="Composed slide not found")

    try:
        layout = Layout.model_validate(composed.layout_json)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(
            status_code=422, detail=f"Invalid layout JSON: {e}",
        ) from e

    # Trigger auto-registration of all built-in widgets.
    import cms.composed.widgets  # noqa: F401

    registry = get_registry()

    # Run the semantic validator first so unknown widgets / bad configs
    # surface a clean 422 before we start touching assets.
    layout_errors = validate_layout(layout, registry)
    if layout_errors:
        raise HTTPException(
            status_code=422,
            detail=(
                "Layout failed validation: "
                + "; ".join(f"{err.code}: {err.message}" for err in layout_errors)
            ),
        )

    # Collect every asset declared by the layout, tracking which widget
    # slug(s) declared each so we can enforce type compatibility.
    declared_ids: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    declaring_slugs: dict[uuid.UUID, set[str]] = {}
    for inst in layout.widgets:
        widget = registry.get(inst.type)
        if widget is None:
            continue
        try:
            cfg = widget.ConfigSchema.model_validate(inst.config)
        except Exception:  # noqa: BLE001 — shape errors already 422'd above
            continue
        for aid in widget.declared_asset_ids(cfg):
            if aid not in seen:
                seen.add(aid)
                declared_ids.append(aid)
            declaring_slugs.setdefault(aid, set()).add(inst.type)

    asset_payloads: dict[uuid.UUID, tuple[bytes, str]] = {}
    sibling_asset_urls: dict[uuid.UUID, str] = {}

    if declared_ids:
        storage_dir = settings.asset_storage_path
        rows = await db.execute(
            select(Asset).where(
                Asset.id.in_(declared_ids), Asset.deleted_at.is_(None),
            ),
        )
        by_id = {a.id: a for a in rows.scalars().all()}

        for aid in declared_ids:
            ref = by_id.get(aid)
            if ref is None:
                raise HTTPException(
                    status_code=422,
                    detail=f"Composed slide references missing asset {aid}",
                )

            # Per-referenced-asset visibility check — a viewer who can see
            # the slide must not be able to inline an asset they can't see.
            if verify_asset is not None:
                await verify_asset(aid)

            if ref.asset_type == AssetType.IMAGE:
                blob = await _read_inline_asset(storage_dir, ref)
                mime, _ = mimetypes.guess_type(ref.filename)
                asset_payloads[aid] = (blob, mime or "application/octet-stream")
            elif ref.asset_type == AssetType.VIDEO:
                slugs = declaring_slugs.get(aid, set())
                if not slugs.issubset(_VIDEO_CAPABLE_SLUGS):
                    bad = sorted(slugs - _VIDEO_CAPABLE_SLUGS)
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Asset {aid} is a video but is used by widget(s) "
                            f"{', '.join(bad)} that cannot render video"
                        ),
                    )
                # Fast reject on recorded size before reading anything.
                if (ref.size_bytes or 0) > _MAX_INLINE_VIDEO_BYTES:
                    raise HTTPException(
                        status_code=422,
                        detail=(
                            f"Video asset {aid} is too large to render "
                            f"({ref.size_bytes} bytes; cap "
                            f"{_MAX_INLINE_VIDEO_BYTES})"
                        ),
                    )
                blob = await _read_inline_asset(
                    storage_dir, ref, max_bytes=_MAX_INLINE_VIDEO_BYTES,
                )
                mime, _ = mimetypes.guess_type(ref.filename)
                b64 = base64.b64encode(blob).decode("ascii")
                sibling_asset_urls[aid] = f"data:{mime or 'video/mp4'};base64,{b64}"
            else:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"Composed slide references asset {aid} of type "
                        f"{ref.asset_type.value!r}; only IMAGE and VIDEO "
                        "assets can be embedded in a composed slide"
                    ),
                )

    def _asset_loader(aid: uuid.UUID) -> tuple[bytes, str]:
        return asset_payloads[aid]

    try:
        built = build_bundle(
            layout,
            registry,
            asset_loader=_asset_loader if asset_payloads else None,
            sibling_asset_urls=sibling_asset_urls or None,
        )
    except BundleValidationError as e:
        raise HTTPException(
            status_code=422,
            detail=(
                "Layout failed validation: "
                + "; ".join(f"{err.code}: {err.message}" for err in e.errors)
            ),
        ) from e

    has_weather = any(inst.type == "weather" for inst in layout.widgets)
    has_rss = any(inst.type == "rss" for inst in layout.widgets)
    # NB: cms_base_url is intentionally left at its None default here.
    # This is the same-origin preview / thumbnail path, so widgets that
    # call back into the CMS (RSS) bake a relative URL that resolves
    # against the preview document's own origin.
    return ComposedRender(
        html_bytes=built.html_bytes, has_weather=has_weather, has_rss=has_rss
    )
