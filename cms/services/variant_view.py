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
