"""Central validation of a device's *final effective* configuration.

This module is the single source of truth for the question **"is the effective
configuration of this device valid?"** It is invoked by every transition that
can change the set of schedules effective for a device — changing its group,
changing its tags, ``pending -> ADOPTED``, bootstrap, and bulk import.

What "effective schedule set" means
-----------------------------------
A device belongs to exactly one group, and that group is the authorization
boundary (see ``cms.services.device_membership``). A schedule targets its
group, optionally narrowed to one *group-scoped tag*. So a device's effective
set is:

    every enabled schedule of the device's group whose ``tag_id`` is NULL or
    is one of the tags the device carries.

Two schedules conflict *for a device* when they are both effective for it,
share the same ``priority``, and their occurrences overlap in time. Because
both are always in the same group, any conflict is by construction visible to
— and fixable by — the same operators. That is precisely the property the
many-to-many attempt (#863) could not provide.

Temporal overlap is delegated to the occurrence engine
(:mod:`cms.services.occurrence`), so midnight-crossing windows, date-boundary
cases, and sub-minute precision are all handled correctly.

Design principles this enforces
-------------------------------
* **Never silently allow a conflict** — an *introduced* equal-priority overlap
  is a blocking problem the caller must reject.
* **Never silently choose** — because equal-priority overlaps are forbidden, the
  runtime winner is always the unique strictly-highest priority; CMS and
  firmware cannot disagree.
* A conflict that already existed *before* the transition (``preexisting``) is
  reported separately so callers can warn / offer repair rather than dead-end a
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
@dataclass(frozen=True)
class DeviceTargeting:
    """What a device is targetable by: its owning group plus its tag set."""

    group_id: uuid.UUID | None
    tag_ids: frozenset[uuid.UUID] = frozenset()





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


def effective_schedules(
    targeting: DeviceTargeting,
    by_group: dict[uuid.UUID, list[Schedule]],
) -> list[Schedule]:
    """The schedules effective for a device with this group + tag set.

    An untagged schedule reaches every device in the group; a tagged schedule
    reaches only the devices carrying that tag. A tag can never resolve outside
    its owning group, so no schedule from another group can appear here.
    """
    if targeting.group_id is None:
        return []
    return [
        s
        for s in by_group.get(targeting.group_id, [])
        if s.tag_id is None or s.tag_id in targeting.tag_ids
    ]


async def validate_device_transition(
    db: AsyncSession,
    *,
    after: dict[str, DeviceTargeting],
    before: dict[str, DeviceTargeting] | None = None,
    composed_slideshow_asset_ids: set[str] | None = None,
) -> SetValidationResult:
    """Validate a proposed group/tag change for a set of devices.

    ``after`` maps each affected ``device_id`` to the group + tags it *will*
    have once the transition is applied; ``before`` maps to what it has now
    (omit for a from-nothing check such as first adoption).

    Only ADOPTED devices are validated — pending/unadopted devices receive no
    sync, so their effective config cannot conflict yet.
    """
    before = before or {}

    group_ids: set[uuid.UUID] = set()
    for targeting in list(after.values()) + list(before.values()):
        if targeting.group_id is not None:
            group_ids.add(targeting.group_id)
    by_group = await _load_enabled_schedules_by_group(db, group_ids)

    device_ids = list(after.keys())
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
    for device_id, after_targeting in after.items():
        device = devices_by_id.get(str(device_id))
        if device is None:
            # Not adopted (or absent) -> nothing is synced to it, skip.
            continue
        before_targeting = before.get(device_id)
        results[str(device_id)] = validate_device_effective_config(
            str(device_id),
            device.device_type,
            device.capabilities,
            before_schedules=(
                effective_schedules(before_targeting, by_group)
                if before_targeting is not None
                else None
            ),
            after_schedules=effective_schedules(after_targeting, by_group),
            composed_slideshow_asset_ids=composed_slideshow_asset_ids,
        )

    return SetValidationResult(devices=results)


async def current_device_targeting(db: AsyncSession, device_id: str) -> DeviceTargeting:
    """Read a device's present group + tag set."""
    from cms.models.device_tag import DeviceTagAssignment

    group_id = (
        await db.execute(select(Device.group_id).where(Device.id == device_id))
    ).scalar_one_or_none()
    tag_ids = (
        await db.execute(
            select(DeviceTagAssignment.tag_id).where(
                DeviceTagAssignment.device_id == device_id
            )
        )
    ).scalars().all()
    return DeviceTargeting(group_id=group_id, tag_ids=frozenset(tag_ids))


def describe_conflicts(pairs: list[ConflictPair]) -> str:
    """A one-line, operator-readable summary of conflicting schedule pairs."""
    return "; ".join(
        f"'{p.schedule_a_name}' vs '{p.schedule_b_name}' (priority {p.priority})"
        for p in pairs
    )
