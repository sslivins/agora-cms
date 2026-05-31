"""Friendly-name resolver for assistant approval cards.

The MCP write-tool schemas take UUID arguments (``device_id``,
``default_asset_id``, …) which makes total sense for the LLM ↔ MCP
boundary but renders as opaque hex blobs in the human approval card.
This module produces a parallel ``display_arguments`` dict that maps
the same keys to human-recognisable strings (device name, asset
display-name, …) so the UI can show ``Pi100`` instead of
``af06ad30-0c1e-45fe-a41c-f6b271396ded``.

Design notes
------------

* **Snapshot at write time.** The resolver runs once when the
  ``ChatPendingApproval`` row is created, and the result is persisted
  on the row.  That means the user sees the friendly names that were
  current *when they were asked to approve* — even if the device is
  later renamed or the asset is deleted before they click Approve.

* **Silent fallback on miss.** Unknown keys (anything not in the
  registry), unparseable UUIDs, and IDs that don't resolve to a row
  are all silently omitted from the output.  The frontend treats a
  missing key as "show the raw UUID" so the user is never blocked.

* **Registry-driven.** Adding a new ID-shaped arg key is a single
  line in :data:`_SINGLE_REGISTRY` / :data:`_PLURAL_REGISTRY`.  No
  changes required in the agent or the router.

* **Batched queries.** All IDs for the same model are collected and
  fetched in one ``WHERE id IN (...)`` query.  A worst-case approval
  with ten ``device_ids`` + three ``asset_ids`` issues two SELECTs,
  not thirteen.

* **No visibility filter.** The action originates from the user who
  is currently being asked to approve it — by construction the agent
  loop already had on-behalf-of-them access to look these IDs up
  (it's how the LLM produced the arguments in the first place).  So
  resolving the names here doesn't leak anything the user couldn't
  see on a normal page load.  If that invariant ever changes (e.g.
  shared threads), this is the place to add a per-user gate.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Iterable, Mapping

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.asset_view import AssetView
from cms.models.device import Device, DeviceGroup
from cms.models.device_profile import DeviceProfile
from cms.models.schedule import Schedule
from cms.models.tag import Tag
from shared.models.asset import Asset

logger = logging.getLogger(__name__)


# (Model, attribute-chain) — the first attribute in the chain that
# evaluates to a non-empty string wins.  Multi-attr chains exist for
# assets because ``display_name`` is user-editable and may be null on
# legacy rows; fall back to ``original_filename`` (set when we
# converted e.g. HEIC→JPG) and ultimately the on-disk filename.
_AssetChain = (Asset, ("display_name", "original_filename", "filename"))

_SINGLE_REGISTRY: dict[str, tuple[type, tuple[str, ...]]] = {
    "device_id": (Device, ("name",)),
    "asset_id": _AssetChain,
    "default_asset_id": _AssetChain,
    "source_asset_id": _AssetChain,
    "group_id": (DeviceGroup, ("name",)),
    "tag_id": (Tag, ("name",)),
    "schedule_id": (Schedule, ("name",)),
    "profile_id": (DeviceProfile, ("name",)),
    "asset_view_id": (AssetView, ("name",)),
    "view_id": (AssetView, ("name",)),
}

_PLURAL_REGISTRY: dict[str, tuple[type, tuple[str, ...]]] = {
    "device_ids": (Device, ("name",)),
    "member_device_ids": (Device, ("name",)),
    "asset_ids": _AssetChain,
    "group_ids": (DeviceGroup, ("name",)),
    "member_group_ids": (DeviceGroup, ("name",)),
    "tag_ids": (Tag, ("name",)),
    "schedule_ids": (Schedule, ("name",)),
}


def _coerce_id(raw: Any) -> str | uuid.UUID | None:
    """Best-effort coercion to an ID we can put into a WHERE id IN (...)
    clause.  Returns ``None`` for anything obviously non-id-shaped.

    We deliberately don't force UUID parsing: ``Device.id`` is a plain
    ``String(64)`` (Pi serial), while the rest of the catalog uses
    ``UUID``.  SQLAlchemy handles the column-type coercion downstream
    for both shapes, so the registry can stay homogeneous.
    """
    if isinstance(raw, uuid.UUID):
        return raw
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or len(s) > 64:
        return None
    # Prefer a real UUID when the string parses as one — SQLAlchemy's
    # UUID column binder requires uuid.UUID, not a plain string.  Plain
    # strings (Pi serials in Device.id) are returned as-is.
    try:
        return uuid.UUID(s)
    except (ValueError, AttributeError):
        return s


def _first_nonempty(row: Any, attr_chain: tuple[str, ...]) -> str | None:
    """Return the first attribute in ``attr_chain`` that is set on
    ``row`` and is a non-empty string."""
    for attr in attr_chain:
        val = getattr(row, attr, None)
        if val is None:
            continue
        text = str(val).strip()
        if text:
            return text
    return None


async def _bulk_load(
    db: AsyncSession,
    model: type,
    ids: Iterable[Any],
) -> dict[Any, Any]:
    """Return ``{id: row}`` for the given model + ids in one query.

    Missing rows are simply absent from the output dict.  ``ids`` may
    be a mix of ``str`` and :class:`uuid.UUID` — SQLAlchemy coerces to
    the column type on the way out.
    """
    seen: list[Any] = []
    dedup: set[str] = set()
    for i in ids:
        key = str(i)
        if key in dedup:
            continue
        dedup.add(key)
        seen.append(i)
    if not seen:
        return {}
    stmt = select(model).where(model.id.in_(seen))  # type: ignore[attr-defined]
    result = await db.execute(stmt)
    return {str(row.id): row for row in result.scalars().all()}


async def resolve_friendly_names(
    db: AsyncSession,
    tool_arguments: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Build a ``display_arguments`` mapping for ``tool_arguments``.

    Returns a dict containing only the keys that successfully resolved
    to a human-readable name.  An empty dict is returned (not ``None``)
    if nothing resolves — the caller decides whether to persist an
    empty dict or ``None``; this keeps the type consistent for tests.

    Plural keys (``..._ids``) resolve to a *list of names* in the
    original list order.  IDs that don't resolve produce ``None`` at
    that position so the list length matches the input — the frontend
    then knows to render the raw UUID for that slot.
    """
    if not tool_arguments:
        return {}

    # Pass 1 — collect everything we need to fetch, grouped by model.
    needed_by_model: dict[type, list[Any]] = {}
    plan_single: list[tuple[str, Any, type, tuple[str, ...]]] = []
    plan_plural: list[
        tuple[str, list[Any], type, tuple[str, ...]]
    ] = []

    def _track(model: type, raw_id: Any) -> None:
        bucket = needed_by_model.setdefault(model, [])
        if not any(str(existing) == str(raw_id) for existing in bucket):
            bucket.append(raw_id)

    for key, raw in tool_arguments.items():
        if key in _SINGLE_REGISTRY:
            model, attrs = _SINGLE_REGISTRY[key]
            parsed = _coerce_id(raw)
            if parsed is None:
                continue
            _track(model, parsed)
            plan_single.append((key, parsed, model, attrs))
        elif key in _PLURAL_REGISTRY and isinstance(raw, list):
            model, attrs = _PLURAL_REGISTRY[key]
            parsed_list: list[Any] = []
            for item in raw:
                pid = _coerce_id(item)
                parsed_list.append(pid)
                if pid is not None:
                    _track(model, pid)
            if any(p is not None for p in parsed_list):
                plan_plural.append((key, parsed_list, model, attrs))

    if not plan_single and not plan_plural:
        return {}

    # Pass 2 — one bulk SELECT per model, swallow lookup errors so a
    # transient DB hiccup never blocks the approval card from being
    # shown at all (worst case: the user sees raw UUIDs, same as
    # before this feature existed).
    loaded: dict[type, dict[Any, Any]] = {}
    for model, ids in needed_by_model.items():
        try:
            loaded[model] = await _bulk_load(db, model, ids)
        except Exception:  # noqa: BLE001
            logger.warning(
                "approval_display.bulk_load_failed model=%s",
                getattr(model, "__name__", repr(model)),
                exc_info=True,
            )
            loaded[model] = {}

    # Pass 3 — build the output.
    display: dict[str, Any] = {}
    for key, pid, model, attrs in plan_single:
        row = loaded.get(model, {}).get(str(pid))
        if row is None:
            continue
        name = _first_nonempty(row, attrs)
        if name:
            display[key] = name

    for key, parsed_list, model, attrs in plan_plural:
        rows = loaded.get(model, {})
        names: list[str | None] = []
        any_resolved = False
        for pid in parsed_list:
            if pid is None:
                names.append(None)
                continue
            row = rows.get(str(pid))
            if row is None:
                names.append(None)
                continue
            name = _first_nonempty(row, attrs)
            names.append(name)
            if name:
                any_resolved = True
        if any_resolved:
            display[key] = names

    return display


__all__ = ["resolve_friendly_names"]
