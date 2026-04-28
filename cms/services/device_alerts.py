"""Severity tag derivation for the /devices triage bar.

The /devices page renders per-row alert chips via the
``device_alert_chips`` Jinja macro. As a fleet grows beyond a handful
of devices, those chips become noise — operators need a top-of-page
triage bar that summarizes "what's broken right now" and lets them
filter the list.

This module is the **single source of truth** for the severity
taxonomy. Both the triage bar (counts + filter) and the per-row
``data-severity-tags`` attribute that drives client-side filtering
derive their tags from :func:`device_severity_tags`.

The chip-rendering macro intentionally stays in Jinja for now (its
inline tooltips and value formatting are template-shaped); a unit
test asserts the two layers agree on which tags fire for a given
device state, so drift is detectable.
"""

from __future__ import annotations

from typing import Iterable


# Public severity tags. The triage bar renders one chip per tag (plus
# the synthetic "all", "needs-attention", and "healthy" buckets which
# are derived from these). Order matches highest-severity-first for
# any future "primary tag" ranking.
SEVERITY_TAGS: tuple[str, ...] = (
    "error",
    "offline",
    "display-off",
    "storage-critical",
    "orphaned",
    "maintenance",
)


# Tags that compose the "Needs Attention" filter. Storage Low is
# intentionally excluded (Critical is the actionable threshold);
# Maintenance is intentionally excluded (it's informational, not an
# outage).
NEEDS_ATTENTION_TAGS: frozenset[str] = frozenset({
    "error",
    "offline",
    "display-off",
    "storage-critical",
    "orphaned",
})


def _is_display_off(d) -> bool:
    """Mirror the macro logic for "no display detected".

    True only when the device is online (offline display state is
    stale) AND either every reported display port is disconnected, or
    the legacy boolean ``display_connected`` is explicitly False with
    no port list.
    """
    if not getattr(d, "is_online", False):
        return False
    ports = getattr(d, "display_ports", None) or []
    if ports:
        # All ports unplugged.
        return all(not p.get("connected") for p in ports)
    # Legacy single-port path.
    return getattr(d, "display_connected", None) is False


def _free_storage_pct(d) -> float | None:
    cap = getattr(d, "storage_capacity_mb", None) or 0
    if cap <= 0:
        return None
    used = getattr(d, "storage_used_mb", None) or 0
    return (cap - used) / cap * 100.0


def device_severity_tags(d, user_perms: Iterable[str] | None = None) -> list[str]:
    """Return the severity tags that apply to a single device.

    Tags returned (subset of :data:`SEVERITY_TAGS`):

    - ``error`` — online + (``error`` set or ``pipeline_state == 'ERROR'``)
    - ``offline`` — adopted device whose websocket is disconnected
    - ``display-off`` — online + every display port unplugged
    - ``storage-critical`` — < 5% free
    - ``orphaned`` — re-flashed device awaiting re-adoption
    - ``maintenance`` — firmware update available or upgrade in progress

    Pending devices are intentionally excluded from every tag — they
    aren't operational yet and live in their own card.

    When *user_perms* is provided, the ``maintenance`` tag is omitted
    for users without ``devices:manage`` since they can't act on it
    (the kebab Update action and the Update badge tooltip both require
    that permission). Pass ``None`` (the default) to get the unfiltered
    tag list — used by tests and any caller that wants the raw view.
    """
    status = getattr(getattr(d, "status", None), "value", None)
    if status == "pending":
        return []

    tags: list[str] = []

    if status == "orphaned":
        tags.append("orphaned")
        # Orphaned devices have no live telemetry worth surfacing as
        # additional tags; the orphaned state subsumes them.
        return tags

    is_online = getattr(d, "is_online", False)

    if is_online:
        if getattr(d, "error", None) or getattr(d, "pipeline_state", None) == "ERROR":
            tags.append("error")
        if _is_display_off(d):
            tags.append("display-off")
    else:
        tags.append("offline")

    free_pct = _free_storage_pct(d)
    if free_pct is not None and free_pct < 5:
        tags.append("storage-critical")

    if getattr(d, "is_upgrading", False) or getattr(d, "update_available", False):
        if user_perms is None or "devices:manage" in user_perms:
            tags.append("maintenance")

    return tags


def is_needs_attention(tags: Iterable[str]) -> bool:
    """True if any tag in *tags* is in the Needs-Attention bucket."""
    return any(t in NEEDS_ATTENTION_TAGS for t in tags)


def fleet_counts(devices, user_perms: Iterable[str] | None = None) -> dict[str, int]:
    """Compute triage-bar counts across a list of decorated devices.

    Pending devices are excluded from every count (including ``all``)
    — they don't represent operational fleet capacity.

    *user_perms* is forwarded to :func:`device_severity_tags`; when
    set, the ``maintenance`` tag is suppressed for users without
    ``devices:manage`` so the corresponding stat tile / triage chip
    reads zero rather than pointing them at devices they can't act on.

    Returned keys:

    - ``all`` — total adopted/orphaned devices
    - ``needs_attention`` — devices with at least one tag in
      :data:`NEEDS_ATTENTION_TAGS`
    - one key per tag in :data:`SEVERITY_TAGS` (with hyphens kept;
      the template references them as ``counts['display-off']`` etc.)
    - ``healthy`` — devices with zero tags
    """
    counts: dict[str, int] = {"all": 0, "needs_attention": 0, "healthy": 0}
    for tag in SEVERITY_TAGS:
        counts[tag] = 0

    for d in devices:
        status = getattr(getattr(d, "status", None), "value", None)
        if status == "pending":
            continue
        counts["all"] += 1
        tags = device_severity_tags(d, user_perms)
        if not tags:
            counts["healthy"] += 1
            continue
        if is_needs_attention(tags):
            counts["needs_attention"] += 1
        for t in tags:
            counts[t] = counts.get(t, 0) + 1

    return counts


# Group-level rollup uses the exact same shape and logic as
# fleet_counts — there is no separate severity taxonomy for groups.
# This alias exists only so callers/templates can express intent
# ("rollup for one group") without duplicating the function.
group_rollup = fleet_counts
