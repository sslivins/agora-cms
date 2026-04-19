"""Helpers for presenting variants in the Library UI.

The Library view collapses the `asset_variants` table to the newest live
(``deleted_at IS NULL``) row per ``(source_asset_id, profile_id)`` slot
so users see exactly what is currently being produced for each profile —
not the stale READY row that is about to be superseded by an in-flight
transcode.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import TypeVar

from shared.models.asset import VariantStatus

_V = TypeVar("_V")


def collapse_to_latest(variants: Iterable[_V]) -> list[_V]:
    """Return one variant per ``profile_id`` — the newest live row.

    Rules:
      * Drop rows with ``deleted_at IS NOT NULL`` (soft-deleted).
      * For each remaining ``profile_id``, keep the row with the
        greatest ``created_at`` (ties broken by ``id`` for determinism).
      * Variants with no ``profile_id`` are dropped (defensive; shouldn't
        happen in practice — the FK is NOT NULL).

    The input objects only need ``profile_id``, ``created_at``,
    ``deleted_at`` and (for tie-breaking) ``id`` attributes.
    """
    latest: dict[object, _V] = {}
    for v in variants:
        if getattr(v, "deleted_at", None) is not None:
            continue
        pid = getattr(v, "profile_id", None)
        if pid is None:
            continue
        cur = latest.get(pid)
        if cur is None:
            latest[pid] = v
            continue
        v_created = getattr(v, "created_at", None)
        cur_created = getattr(cur, "created_at", None)
        if v_created is not None and cur_created is not None:
            if v_created > cur_created:
                latest[pid] = v
            elif v_created == cur_created and str(getattr(v, "id", "")) > str(
                getattr(cur, "id", "")
            ):
                latest[pid] = v
    return list(latest.values())


def collapse_to_ready(variants: Iterable[_V]) -> list[_V]:
    """Return one READY variant per ``profile_id`` (newest wins).

    Mirror of :func:`collapse_to_latest` but restricted to rows whose
    ``status`` is ``VariantStatus.READY``. Soft-deleted rows are dropped.

    Used by the "is this asset playable for every profile?" check that
    gates device/group/schedule asset selection (issue #201). A profile
    with an older READY variant and a newer PROCESSING row for a fresh
    re-transcode still appears here, because the old blob can still
    serve the device until the new one lands.
    """
    latest: dict[object, _V] = {}
    for v in variants:
        if getattr(v, "deleted_at", None) is not None:
            continue
        if getattr(v, "status", None) != VariantStatus.READY:
            continue
        pid = getattr(v, "profile_id", None)
        if pid is None:
            continue
        cur = latest.get(pid)
        if cur is None:
            latest[pid] = v
            continue
        v_created = getattr(v, "created_at", None)
        cur_created = getattr(cur, "created_at", None)
        if v_created is not None and cur_created is not None:
            if v_created > cur_created:
                latest[pid] = v
            elif v_created == cur_created and str(getattr(v, "id", "")) > str(
                getattr(cur, "id", "")
            ):
                latest[pid] = v
    return list(latest.values())


def is_asset_ready(variants: Iterable[_V]) -> tuple[bool, str | None]:
    """Return whether an asset is ready for new splash / schedule assignment.

    An asset is considered **ready** iff, for every profile that has any
    live (non-deleted) variant row, at least one of that profile's live
    variants is in :attr:`VariantStatus.READY`.

    Corollaries:
      * Assets with no variants at all (webpage / stream / upload not
        yet dispatched) are ready — nothing to gate on.
      * A profile that has been added globally but for which no variant
        row exists for this asset yet is ignored (not worth tracking).
      * Re-transcode case (old READY + new PROCESSING for the same
        profile) stays ready — the old variant still serves traffic.

    Returns ``(ready, reason)``. ``reason`` is ``None`` when ready, else
    a short human-readable suffix — ``"transcoding…"`` when any blocking
    profile still has PENDING/PROCESSING work, or ``"transcode failed"``
    when the only non-READY variants left for that profile are FAILED.
    """
    # Collect live variants; bail early if nothing to check.
    live: list[_V] = [
        v for v in variants if getattr(v, "deleted_at", None) is None
    ]
    if not live:
        return True, None

    # Group by profile_id. Variants with no profile_id are defensive-skipped.
    by_profile: dict[object, list[_V]] = {}
    for v in live:
        pid = getattr(v, "profile_id", None)
        if pid is None:
            continue
        by_profile.setdefault(pid, []).append(v)

    if not by_profile:
        return True, None

    any_in_flight = False
    any_unready_profile = False
    for pid, vs in by_profile.items():
        if any(getattr(v, "status", None) == VariantStatus.READY for v in vs):
            continue
        any_unready_profile = True
        if any(
            getattr(v, "status", None)
            in (VariantStatus.PENDING, VariantStatus.PROCESSING)
            for v in vs
        ):
            any_in_flight = True
            break  # in-flight dominates failed for the user-facing reason

    if not any_unready_profile:
        return True, None
    return False, ("transcoding…" if any_in_flight else "transcode failed")

