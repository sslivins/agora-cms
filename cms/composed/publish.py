"""Phase 1A: publish a composed slide's layout to the asset cache.

Renders ``ComposedSlide.layout_json`` to a self-contained HTML bundle
via :func:`cms.composed.bundle.build_bundle`, writes the bytes to the
shared asset storage path, and updates the bound :class:`Asset` row's
``filename`` / ``size_bytes`` / ``checksum`` so the existing device
sync pipeline picks it up automatically.

Phase 1A is explicit-publish only — no auto-rebuild on referenced
asset changes (that affordance lives in the Phase 2 editor UI).
Calling :func:`publish_composed_slide` flips ``is_draft`` to ``False``
and records ``bundle_built_at`` + ``bundle_source_asset_ids`` for the
stale-bundle detector to use later.

The publish is idempotent on content: if the freshly-built bundle's
SHA matches the asset's current checksum, we skip the disk write and
only update ``bundle_built_at`` (so the editor's "last built" UI
still ticks).  Because the bundle filename is content-addressed
(``composed-{asset_id}-{sha[:12]}.html``), re-publishing identical
content also re-points at the same file rather than orphaning a new
one.
"""

from __future__ import annotations

import mimetypes
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.composed.bundle import BundleValidationError, build_bundle
from cms.composed.registry import get_registry
from cms.composed.schema import Layout
from cms.models.composed_slide import ComposedSlide
from shared.models.asset import Asset, AssetType
from shared.services.storage import get_storage


class PublishError(Exception):
    """Raised when a composed slide cannot be published.

    Wraps the underlying cause (missing asset row, missing composed
    slide row, invalid layout JSON, validation failure).  Caller is
    expected to translate this into an HTTP 4xx / friendly error.
    """


@dataclass(frozen=True)
class PublishResult:
    """Outcome of a publish call, returned for logging / UI."""

    asset_id: uuid.UUID
    composed_slide_id: uuid.UUID
    filename: str
    checksum: str
    size_bytes: int
    rebuilt: bool  # False if content matched existing checksum (no-op write)
    bundle_built_at: datetime


def _bundle_filename(asset_id: uuid.UUID, sha256_hex: str) -> str:
    # Content-addressed but namespaced by asset, so two different
    # composed slides with byte-identical bundles still land in
    # different files (avoids any future collision-by-coincidence and
    # makes "which composed slide owns this file?" trivial to answer
    # from the filename alone).
    return f"composed-{asset_id}-{sha256_hex[:12]}.html"


async def publish_composed_slide(
    asset_id: uuid.UUID, db: AsyncSession,
) -> PublishResult:
    """Build and publish the bundle for the composed slide bound to ``asset_id``.

    On success:
      * The asset row's ``filename`` / ``size_bytes`` / ``checksum``
        point at the freshly-written bundle.
      * The composed slide row's ``is_draft`` is False,
        ``bundle_built_at`` is now (UTC), and
        ``bundle_source_asset_ids`` is the list returned by the bundle
        builder.
      * ``db.commit()`` is **not** called — the caller controls the
        transaction boundary (matches the existing asset-router
        convention).
    """
    asset = await db.get(Asset, asset_id)
    if asset is None:
        raise PublishError(f"Asset {asset_id} not found")
    if asset.asset_type != AssetType.COMPOSED:
        raise PublishError(
            f"Asset {asset_id} is {asset.asset_type.value}, not composed",
        )

    cs_result = await db.execute(
        select(ComposedSlide).where(ComposedSlide.asset_id == asset_id),
    )
    composed = cs_result.scalar_one_or_none()
    if composed is None:
        raise PublishError(f"No composed slide row bound to asset {asset_id}")

    # Layout JSON → Pydantic.  This will raise pydantic.ValidationError
    # on shape problems; the caller can catch it or let it bubble.
    try:
        layout = Layout.model_validate(composed.layout_json)
    except Exception as e:  # noqa: BLE001 — re-wrap for caller clarity
        raise PublishError(f"Invalid layout JSON: {e}") from e

    # Trigger auto-registration of all built-in widgets.
    import cms.composed.widgets  # noqa: F401

    registry = get_registry()

    # Pre-fetch every asset declared by any widget in the layout.
    # ``build_bundle`` runs sync, so we resolve all (bytes, mime) pairs
    # here (async-friendly DB + disk reads) and hand the builder a
    # dict-backed synchronous closure.  Widgets that declare an asset
    # they can't legitimately reference will be caught by validation
    # before we get here, but missing-on-disk slips through to this
    # loop and surfaces as a PublishError.
    from cms.auth import get_settings as _get_settings

    storage_dir = _get_settings().asset_storage_path

    declared_ids: list[uuid.UUID] = []
    seen_declared: set[uuid.UUID] = set()
    for inst in layout.widgets:
        widget = registry.get(inst.type)
        if widget is None:
            # validate_layout (run inside build_bundle) will reject
            # this with a clearer error; skip here so we don't crash
            # before that gets a chance to run.
            continue
        cfg = widget.ConfigSchema.model_validate(inst.config)
        for aid in widget.declared_asset_ids(cfg):
            if aid not in seen_declared:
                seen_declared.add(aid)
                declared_ids.append(aid)

    asset_payloads: dict[uuid.UUID, tuple[bytes, str]] = {}
    if declared_ids:
        rows = await db.execute(
            select(Asset).where(Asset.id.in_(declared_ids)),
        )
        by_id = {a.id: a for a in rows.scalars().all()}
        for aid in declared_ids:
            ref = by_id.get(aid)
            if ref is None:
                raise PublishError(
                    f"Composed slide references missing asset {aid}",
                )
            ref_path = storage_dir / ref.filename
            try:
                blob = ref_path.read_bytes()
            except FileNotFoundError as e:
                raise PublishError(
                    f"Asset {aid} file missing on disk: {ref.filename}",
                ) from e
            mime, _ = mimetypes.guess_type(ref.filename)
            asset_payloads[aid] = (blob, mime or "application/octet-stream")

    def _asset_loader(aid: uuid.UUID) -> tuple[bytes, str]:
        # build_bundle only calls this for IDs in ``declared_ids``,
        # which is exactly what we pre-fetched above; a KeyError here
        # would mean a widget bypassed declared_asset_ids() and is a
        # programming error worth surfacing loudly.
        return asset_payloads[aid]

    try:
        built = build_bundle(
            layout,
            registry,
            asset_loader=_asset_loader if declared_ids else None,
        )
    except BundleValidationError as e:
        raise PublishError(
            "Layout failed validation: "
            + "; ".join(f"{err.code}: {err.message}" for err in e.errors),
        ) from e

    filename = _bundle_filename(asset_id, built.sha256_hex)
    storage = get_storage()
    storage_dir.mkdir(parents=True, exist_ok=True)
    dest = storage_dir / filename

    rebuilt = asset.checksum != built.sha256_hex
    if rebuilt:
        dest.write_bytes(built.html_bytes)
        await storage.on_file_stored(filename)

        asset.filename = filename
        asset.size_bytes = len(built.html_bytes)
        asset.checksum = built.sha256_hex

    now = datetime.now(timezone.utc)
    composed.is_draft = False
    composed.bundle_built_at = now
    composed.bundle_source_asset_ids = list(built.source_asset_ids)

    await db.flush()

    return PublishResult(
        asset_id=asset_id,
        composed_slide_id=composed.id,
        filename=asset.filename,
        checksum=asset.checksum,
        size_bytes=asset.size_bytes,
        rebuilt=rebuilt,
        bundle_built_at=now,
    )
