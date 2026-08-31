"""Central validation of a device's *final effective* configuration.

Stage 1 of the device-groups many-to-many rework (issue #863). This module is
the single source of truth for the question **"is the effective configuration
of this device valid?"** It is designed to be invoked by every transition that
can change the set of schedules effective for a device — membership
add/remove/replace, ``pending -> ADOPTED``, ``PATCH status``, bootstrap, bulk
import, and capability/profile changes.

Key reframing versus the legacy per-group check
------------------------------------------------
The legacy :func:`cms.services.scheduler.schedules_conflict` only compared two
schedules that shared the *same group*. Once a device may belong to more than
one group, that framing is wrong: the thing that actually matters is a
**device's effective schedule set** — the union of the schedules of every group
the device belongs to. Two schedules conflict *for a device* when they are both
effective for that device, share the same ``priority``, and their occurrences
overlap in time.

Temporal overlap is delegated to the occurrence engine
(:mod:`cms.services.occurrence`), so midnight-crossing windows, date-boundary
cases, and sub-minute precision are all handled correctly (Stage 0).

Design principles this enforces (from the locked design doc)
------------------------------------------------------------
* **Never silently allow a conflict** — an *introduced* equal-priority overlap
  is a blocking problem the caller must reject.
* **Never silently choose** — because equal-priority overlaps are forbidden, the
  runtime winner is always the unique strictly-highest priority; CMS and
  firmware cannot disagree.
* A conflict that already existed *before* the transition (``preexisting``) is
  reported separately so callers can warn / offer repair rather than block a
  change that did not introduce it.

This module contains a **pure core** (no DB, fully unit-testable) plus thin
async orchestrators that load the data and run the core set-based.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from itertools import combinations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.models.asset import AssetType
from cms.models.device import Device, DeviceStatus
from cms.models.schedule import Schedule
from cms.schemas.protocol import (
    CAPABILITY_SLIDESHOW_COMPOSED_V1,
    CAPABILITY_SLIDESHOW_V1,
)
from cms.services.occurrence import schedules_overlap

# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ConflictPair:
    """An equal-priority, time-overlapping pair of schedules for one device."""

    schedule_a_id: str
    schedule_a_name: str
    schedule_b_id: str
    schedule_b_name: str
    priority: int

    @property
    def key(self) -> tuple[str, str]:
        """Order-independent identity of the pair (for set membership / diffing)."""
        return tuple(sorted((self.schedule_a_id, self.schedule_b_id)))


@dataclass(frozen=True)
class CapabilityFailure:
    """A schedule whose asset requires a capability the device lacks."""

    schedule_id: str
    schedule_name: str
    asset_type: str
    required_capability: str
    reason: str


@dataclass
class DeviceValidationResult:
    """Structured validation outcome for a single device."""

    device_id: str
    introduced_conflicts: list[ConflictPair] = field(default_factory=list)
    preexisting_conflicts: list[ConflictPair] = field(default_factory=list)
    capability_failures: list[CapabilityFailure] = field(default_factory=list)

    @property
    def is_blocked(self) -> bool:
        """True when the transition must be rejected.

        A transition is rejected if it *introduces* a conflict or leaves the
        device with a schedule its firmware cannot render. Pre-existing
        conflicts are intentionally NOT blocking here — they are surfaced for
        warn/repair so a change that did not create them isn't dead-ended.
        """
        return bool(self.introduced_conflicts or self.capability_failures)

    @property
    def has_warnings(self) -> bool:
        return bool(self.preexisting_conflicts)


@dataclass
class SetValidationResult:
    """Validation outcome for a set of devices touched by one transition."""

    devices: dict[str, DeviceValidationResult] = field(default_factory=dict)

    @property
    def is_blocked(self) -> bool:
        return any(r.is_blocked for r in self.devices.values())

    @property
    def blocked_devices(self) -> list[DeviceValidationResult]:
        return [r for r in self.devices.values() if r.is_blocked]

    def all_introduced_conflicts(self) -> list[ConflictPair]:
        seen: dict[tuple[str, str], ConflictPair] = {}
        for r in self.devices.values():
            for c in r.introduced_conflicts:
                seen[c.key] = c
        return list(seen.values())


# --------------------------------------------------------------------------- #
# Pure core (no DB)
# --------------------------------------------------------------------------- #


def find_effective_conflicts(
    schedules: list[Schedule],
) -> dict[tuple[str, str], ConflictPair]:
    """Return every equal-priority, time-overlapping pair among ``schedules``.

    ``schedules`` is a device's *effective* set (already unioned across all the
    device's groups). Only enabled schedules participate — a disabled schedule
    emits nothing to the device, so it cannot conflict. Result is keyed by the
    order-independent pair key so results can be diffed across transitions.
    """
    enabled = [s for s in schedules if s.enabled]

    by_priority: dict[int, list[Schedule]] = {}
    for s in enabled:
        by_priority.setdefault(s.priority, []).append(s)

    conflicts: dict[tuple[str, str], ConflictPair] = {}
    for priority, bucket in by_priority.items():
        for a, b in combinations(bucket, 2):
            if schedules_overlap(a, b):
                pair = ConflictPair(
                    schedule_a_id=str(a.id),
                    schedule_a_name=a.name,
                    schedule_b_id=str(b.id),
                    schedule_b_name=b.name,
                    priority=priority,
                )
                conflicts[pair.key] = pair
    return conflicts


def diff_conflicts(
    before: dict[tuple[str, str], ConflictPair],
    after: dict[tuple[str, str], ConflictPair],
) -> tuple[list[ConflictPair], list[ConflictPair]]:
    """Split ``after`` conflicts into (introduced, preexisting) versus ``before``."""
    introduced = [pair for key, pair in after.items() if key not in before]
    preexisting = [pair for key, pair in after.items() if key in before]
    return introduced, preexisting


def _is_pi5_compatible(device_type: str | None) -> bool:
    """Whether a device type string indicates a Pi 5 / Compute Module 5.

    Mirrors ``cms.routers.schedules._is_pi5_compatible``; kept here so the
    service layer has no dependency on the router layer (the router should
    migrate onto this copy in a later stage).
    """
    if not device_type:
        return False
    dt_lower = device_type.lower()
    return "pi 5" in dt_lower or "compute module 5" in dt_lower


def capability_failures_for_device(
    device_type: str | None,
    capabilities: list[str] | None,
    schedules: list[Schedule],
    composed_slideshow_asset_ids: set[str] | None = None,
) -> list[CapabilityFailure]:
    """Return the effective schedules whose asset this device cannot render.

    Reframes the legacy per-group capability gates
    (``_validate_webpage_group`` / ``_validate_slideshow_group``) as a
    per-device check:

    * webpage / live-stream asset -> device must be a Pi 5 or newer;
    * slideshow asset -> device must advertise ``slideshow_v1``;
    * slideshow containing a COMPOSED member -> device must additionally
      advertise ``slideshow_composed_v1``.

    ``composed_slideshow_asset_ids`` is supplied by the async orchestrator
    (composed-member detection needs the DB); when omitted, the composed check
    is skipped.
    """
    caps = set(capabilities or [])
    composed_ids = composed_slideshow_asset_ids or set()
    failures: list[CapabilityFailure] = []

    for s in schedules:
        if not s.enabled:
            continue
        asset = getattr(s, "asset", None)
        if asset is None:
            continue
        asset_type = asset.asset_type

        if asset_type in (AssetType.WEBPAGE, AssetType.STREAM):
            if not _is_pi5_compatible(device_type):
                failures.append(
                    CapabilityFailure(
                        schedule_id=str(s.id),
                        schedule_name=s.name,
                        asset_type=asset_type.value,
                        required_capability="raspberry_pi_5",
                        reason=(
                            f"'{asset.filename}' requires a Raspberry Pi 5 or newer; "
                            f"device type is {device_type or 'unknown'}."
                        ),
                    )
                )
        elif asset_type == AssetType.SLIDESHOW:
            if CAPABILITY_SLIDESHOW_V1 not in caps:
                failures.append(
                    CapabilityFailure(
                        schedule_id=str(s.id),
                        schedule_name=s.name,
                        asset_type=asset_type.value,
                        required_capability=CAPABILITY_SLIDESHOW_V1,
                        reason=(
                            f"'{asset.filename}' is a slideshow but the device does not "
                            f"advertise '{CAPABILITY_SLIDESHOW_V1}'."
                        ),
                    )
                )
            elif str(asset.id) in composed_ids and (
                CAPABILITY_SLIDESHOW_COMPOSED_V1 not in caps
            ):
                failures.append(
                    CapabilityFailure(
                        schedule_id=str(s.id),
                        schedule_name=s.name,
                        asset_type=asset_type.value,
                        required_capability=CAPABILITY_SLIDESHOW_COMPOSED_V1,
                        reason=(
                            f"'{asset.filename}' contains a composed slide but the device "
                            f"does not advertise '{CAPABILITY_SLIDESHOW_COMPOSED_V1}'."
                        ),
                    )
                )

    return failures


def validate_device_effective_config(
    device_id: str,
    device_type: str | None,
    capabilities: list[str] | None,
    *,
    before_schedules: list[Schedule] | None,
    after_schedules: list[Schedule],
    composed_slideshow_asset_ids: set[str] | None = None,
) -> DeviceValidationResult:
    """Validate one device's effective config for a proposed transition.

    ``after_schedules`` is the device's effective schedule set *if the
    transition is applied*; ``before_schedules`` is the set as it stands now
    (pass ``None`` for a from-nothing check such as first adoption). Conflicts
    present only in the after-state are *introduced* (blocking); conflicts
    present in both are *preexisting* (warn/repair).
    """
    after_conflicts = find_effective_conflicts(after_schedules)
    before_conflicts = (
        find_effective_conflicts(before_schedules) if before_schedules is not None else {}
    )
    introduced, preexisting = diff_conflicts(before_conflicts, after_conflicts)

    cap_failures = capability_failures_for_device(
        device_type, capabilities, after_schedules, composed_slideshow_asset_ids
    )

    return DeviceValidationResult(
        device_id=device_id,
        introduced_conflicts=introduced,
        preexisting_conflicts=preexisting,
        capability_failures=cap_failures,
    )


# --------------------------------------------------------------------------- #
# Async orchestration (loads data, runs the pure core set-based)
# --------------------------------------------------------------------------- #


async def _load_enabled_schedules_by_group(
    db: AsyncSession, group_ids: set[uuid.UUID]
) -> dict[uuid.UUID, list[Schedule]]:
    """Load enabled schedules (with their asset) for each group in one query."""
    if not group_ids:
        return {}
    result = await db.execute(
        select(Schedule)
        .where(Schedule.group_id.in_(group_ids), Schedule.enabled == True)  # noqa: E712
        .options(selectinload(Schedule.asset))
    )
    by_group: dict[uuid.UUID, list[Schedule]] = {}
    for s in result.scalars().all():
        by_group.setdefault(s.group_id, []).append(s)
    return by_group


def _union_schedules(
    group_ids: set[uuid.UUID],
    by_group: dict[uuid.UUID, list[Schedule]],
) -> list[Schedule]:
    """Deduplicated union of the schedules of ``group_ids`` (by schedule id)."""
    seen: dict[str, Schedule] = {}
    for gid in group_ids:
        for s in by_group.get(gid, []):
            seen[str(s.id)] = s
    return list(seen.values())


async def validate_device_group_transition(
    db: AsyncSession,
    *,
    after_membership: dict[str, set[uuid.UUID]],
    before_membership: dict[str, set[uuid.UUID]] | None = None,
    composed_slideshow_asset_ids: set[str] | None = None,
) -> SetValidationResult:
    """Validate a proposed group-membership transition for a set of devices.

    ``after_membership`` maps each affected ``device_id`` to the set of group
    ids it *will* belong to after the transition; ``before_membership`` maps to
    the set it belongs to *now* (omit for a from-nothing check). The mapping is
    representation-agnostic, so this works both before the join table exists
    (single-group) and after (multi-group).

    Only ADOPTED devices are validated — pending/unadopted devices receive no
    sync, so their effective config cannot conflict yet.
    """
    before_membership = before_membership or {}

    # One query loads schedules for every group referenced by either side.
    all_group_ids: set[uuid.UUID] = set()
    for gids in after_membership.values():
        all_group_ids |= gids
    for gids in before_membership.values():
        all_group_ids |= gids
    by_group = await _load_enabled_schedules_by_group(db, all_group_ids)

    device_ids = list(after_membership.keys())
    devices_by_id: dict[str, Device] = {}
    if device_ids:
        dres = await db.execute(
            select(Device).where(
                Device.id.in_(device_ids),
                Device.status == DeviceStatus.ADOPTED,
            )
        )
        for d in dres.scalars().all():
            devices_by_id[str(d.id)] = d

    results: dict[str, DeviceValidationResult] = {}
    for device_id, after_gids in after_membership.items():
        device = devices_by_id.get(str(device_id))
        if device is None:
            # Not adopted (or absent) -> nothing is synced to it, skip.
            continue
        after_schedules = _union_schedules(after_gids, by_group)
        before_gids = before_membership.get(device_id)
        before_schedules = (
            _union_schedules(before_gids, by_group) if before_gids is not None else None
        )
        results[str(device_id)] = validate_device_effective_config(
            str(device_id),
            device.device_type,
            device.capabilities,
            before_schedules=before_schedules,
            after_schedules=after_schedules,
            composed_slideshow_asset_ids=composed_slideshow_asset_ids,
        )

    return SetValidationResult(devices=results)
