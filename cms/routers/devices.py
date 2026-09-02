"""Device management API routes."""

from collections import defaultdict
import uuid
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import (
    assert_authority_over_group_set,
    build_device_read_scope_clause,
    can_manage_group_membership,
    get_device_group_ids,
    get_settings,
    get_user_group_ids,
    require_auth,
    require_permission,
    verify_resource_group_access,
)
from cms.database import get_db
from cms.permissions import (
    DEVICES_READ, DEVICES_WRITE, DEVICES_MANAGE,
    GROUPS_READ, GROUPS_WRITE,
)
from cms.models.asset import Asset
from shared.models.asset import AssetType
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_group_membership import DeviceGroupMembership
from cms.models.device_profile import DeviceProfile
from cms.models.schedule import Schedule
from cms.schemas.device import (
    AdoptRequest,
    DeviceGroupAddRequest,
    DeviceGroupCreate,
    DeviceGroupMembershipMutationOut,
    DeviceGroupOut,
    DeviceGroupReplaceRequest,
    DeviceScheduleStatusOut,
    DeviceGroupSummary,
    DeviceGroupUpdate,
    DeviceOut,
    DeviceScheduleMatchSummary,
    DeviceUpdate,
    SetPasswordRequest,
    ToggleRequest,
)
from cms.schemas.protocol import ConfigMessage, FactoryResetMessage, OSUpdateDispatchMessage, RebootMessage, SyncMessage, WipeAssetsMessage
from cms.services.transport import get_transport
from cms.services.scheduler import (
    _resolve_group_default_asset,
    get_device_schedule_status,
    push_sync_to_device,
)
from cms.services.audit_service import audit_log, compute_diff
from cms.services.asset_readiness import require_asset_ready
from cms.services.device_membership import (
    DeviceMembershipChange,
    add_device_to_group,
    effective_device_group_rows_subquery,
    remove_device_from_group,
    replace_device_group_memberships,
    set_single_group_membership,
)
from cms.services.bundle_checker import check_now, get_latest_bundle, get_latest_os_version, is_os_update_available
from cms.models.agora_os_channel_bundle import CHANNEL_PRERELEASE, CHANNEL_STABLE, CHANNELS

router = APIRouter(prefix="/api/devices", dependencies=[Depends(require_auth)])

# Separate router for device-originated endpoints — these authenticate
# via X-Device-API-Key, so they must NOT inherit the browser-session
# `require_auth` dependency from the main devices router.
device_originated_router = APIRouter(prefix="/api/devices", tags=["devices (device-originated)"])

# Stage 4 (#344): the in-memory `_upgrading` set was replaced with the
# `devices.upgrade_started_at` column + TTL, so upgrade state is visible
# across replicas and survives restarts.  The timestamp doubles as a
# claim token — the upgrade endpoint captures the value written by its
# atomic CAS and any cleanup compares against that exact timestamp so
# we can't accidentally clear a successor's claim.  ``UPGRADE_TTL`` is
# the max time we'll treat an in-flight upgrade as still valid before
# letting another upgrade request reclaim it (covers stuck reboots).
UPGRADE_TTL = timedelta(minutes=15)

# Issue agora-cms#511: when the send-to-device call fails (502), hold a
# cooldown for this short window before allowing another claim. Without
# it, a double-click while the first send is in flight returns back-to-
# back 502s instead of "give the first one a moment". Stored in its own
# ``upgrade_cooldown_until`` column rather than backdating
# ``upgrade_started_at`` so ``_is_upgrading()`` stays semantically
# correct -- a device that just hit a 502 is *not* upgrading, and the
# UI badge must reflect that throughout the cooldown.
SEND_FAILURE_COOLDOWN = timedelta(seconds=10)

# Issue agora-cms#574 — how recent a device's last lifecycle event has
# to be before we still trust ``devices.ota_*`` to reflect a live OTA.
# Devices emit events on every FSM transition + every 2s during long
# phases (download / extract), so 2 minutes is comfortably looser than
# the slowest healthy cadence while still falling off quickly if the
# device drops without a terminal event (kernel panic mid-extract,
# tryboot that never confirms, etc.).
OTA_FRESH_TTL = timedelta(minutes=2)

# Issue agora-cms#626 — how long a device may sit in the ``tryboot``
# phase before we treat it as stuck mid-upgrade.  The device emits
# ``tryboot_initiated`` right before rebooting into the new slot and we
# expect ``slot_confirmed`` / ``promoted`` (success) or ``failed`` /
# ``declined`` (failure) within at most a few minutes -- the actual
# reboot + slot-mgr aging gate on the device is bounded at ~5 minutes
# by default.  If 15 minutes pass with no terminal event, the device's
# slot-mgr / os-updater are stuck (see ``sslivins/agora#243``) and any
# upgrade dispatch the CMS sends will be silently rejected by the
# on-device state machine.  We surface this as ``upgrade_stuck`` on
# ``DeviceOut`` so the UI can warn the operator and the upgrade
# endpoint can refuse new claims with a clear 409 instead of silently
# no-op'ing on the device.
STUCK_TRYBOOT_TTL = timedelta(minutes=15)


def _as_utc(dt: datetime) -> datetime:
    """Promote a naive ``datetime`` to UTC.

    Postgres ``timestamptz`` columns round-trip as tz-aware datetimes,
    but the SQLite test backend strips the tzinfo on read.  Comparing
    against ``datetime.now(timezone.utc)`` (always aware) would then
    raise ``TypeError: can't subtract offset-naive and offset-aware
    datetimes``.  All ``upgrade_started_at`` / ``ota_updated_at`` writes
    in production are UTC, so naive ⇒ UTC is the right normalization.
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _is_upgrading(device: Device, *, now: datetime | None = None) -> bool:
    """Return whether *device* has an active upgrade claim within TTL."""
    if device.upgrade_started_at is None:
        return False
    ref = now or datetime.now(timezone.utc)
    return (ref - _as_utc(device.upgrade_started_at)) < UPGRADE_TTL


def _is_upgrade_stuck(device: Device, *, now: datetime | None = None) -> bool:
    """Return whether *device* is stuck mid-tryboot (issue agora-cms#626).

    True iff the last lifecycle event we received was
    ``tryboot_initiated`` AND it landed more than ``STUCK_TRYBOOT_TTL``
    ago without any subsequent ``slot_confirmed`` / ``promoted`` /
    ``failed`` / ``declined`` event clearing the row.  Reads the raw
    ``ota_phase`` / ``ota_updated_at`` columns -- NOT the
    ``_ota_fields_for_out`` projection, which already zeroes after
    ``OTA_FRESH_TTL=2min`` for live-progress purposes.  The stuck flag
    is precisely the case where the projection has gone stale because
    no further events arrived.
    """
    if device.ota_phase != "ota_tryboot_initiated":
        return False
    if device.ota_updated_at is None:
        return False
    ref = now or datetime.now(timezone.utc)
    return (ref - _as_utc(device.ota_updated_at)) >= STUCK_TRYBOOT_TTL


def _ota_fields_for_out(device: Device, *, now: datetime | None = None) -> dict:
    """Return staleness-gated ``ota_*`` kwargs for ``DeviceOut``.

    All five UI-facing OTA fields are returned NULL when the device's
    last lifecycle event is older than ``OTA_FRESH_TTL`` — that's
    the safety net for OTAs that stall without a terminal event
    (kernel panic, network drop, tryboot revert).  Without this gate
    a device that hung mid-extract would render a stuck "Extracting
    rootfs 47%" forever; with it, the badge falls back to the legacy
    "Upgrading…" chip (driven by ``is_upgrading``) and eventually
    clears at ``UPGRADE_TTL``.
    """
    if device.ota_updated_at is None:
        return {
            "ota_phase": None, "ota_label": None, "ota_pct": None,
            "ota_bytes_done": None, "ota_bytes_total": None,
        }
    ref = now or datetime.now(timezone.utc)
    if (ref - _as_utc(device.ota_updated_at)) >= OTA_FRESH_TTL:
        return {
            "ota_phase": None, "ota_label": None, "ota_pct": None,
            "ota_bytes_done": None, "ota_bytes_total": None,
        }
    return {
        "ota_phase": device.ota_phase,
        "ota_label": device.ota_label,
        "ota_pct": device.ota_pct,
        "ota_bytes_done": device.ota_bytes_done,
        "ota_bytes_total": device.ota_bytes_total,
    }

# Device model columns that are *also* passed explicitly as kwargs when
# building a ``DeviceOut`` from the live-state + ORM row merge below.
# We exclude them from the ``**{c.key: getattr(d, c.key) ...}`` splat so
# Pydantic doesn't raise ``got multiple values for keyword argument``.
# These are the Stage 2c telemetry columns — they live on the Device row
# now, but the construction paths below still override them with the
# live_states dict produced from ``get_transport().get_all_states()``
# (which itself reads from the same DB row in Stage 2c; Stage 4 will
# collapse this into a single read).
_DEVICE_OUT_OVERLAP_COLUMNS = {
    "online",
    "connection_id",
    "last_status_ts",
    "group_id",
    "cpu_temp_c",
    "load_avg",
    "uptime_seconds",
    "mode",
    "asset",
    "pipeline_state",
    "playback_started_at",
    "playback_position_ms",
    "error",
    "error_since",
    "ssh_enabled",
    "local_api_enabled",
    "display_connected",
    "display_ports",
    "ip_address",
    # OTA progress columns are excluded from the splat so the staleness
    # gate in ``_ota_fields_for_out`` is the only path that writes them
    # into the ``DeviceOut`` — preserves the invariant that the UI
    # never sees stale OTA state past OTA_FRESH_TTL.
    "ota_phase",
    "ota_label",
    "ota_pct",
    "ota_bytes_done",
    "ota_bytes_total",
    "ota_updated_at",
}


def _latest_for_device(
    device: Device, latest_stable: Optional[str], latest_prerelease: Optional[str]
) -> Optional[str]:
    """Resolve the latest available OS version for a device's channel.

    Devices on the ``prerelease`` channel compare against the newest
    release (prereleases included); everyone else compares against the
    newest stable (non-prerelease) release.
    """
    if getattr(device, "update_channel", CHANNEL_STABLE) == CHANNEL_PRERELEASE:
        return latest_prerelease
    return latest_stable


def _device_row_kwargs(device: Device) -> dict:
    """Return ``DeviceOut`` kwargs drawn from the ORM row.

    Excludes columns that are supplied as explicit kwargs (live state,
    presence) at every ``DeviceOut(...)`` call site.
    """
    return {
        c.key: getattr(device, c.key)
        for c in Device.__table__.columns
        if c.key not in _DEVICE_OUT_OVERLAP_COLUMNS
    }


async def _get_device_with_access(
    device_id: str, request: Request, db: AsyncSession,
) -> Device:
    """Fetch a device by ID and verify the current user has group access."""
    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    user = getattr(request.state, "user", None)
    if user:
        await verify_resource_group_access(user, db, await get_device_group_ids(device, db))
    return device


async def _verify_membership_change_access(
    user,
    device: Device,
    db: AsyncSession,
    *,
    target_group_id: uuid.UUID | None,
) -> None:
    """Require authority over every group touched by a membership replace.

    Stage 4 prepares for Stage 6's dedicated membership CRUD by enforcing the
    same invariant on today's scalar ``group_id`` write paths: if a change adds,
    removes, or replaces memberships, the actor must be able to manage every
    affected group — which in turn implies schedule read/write there.
    """
    touched_group_ids = await get_device_group_ids(device, db)
    if target_group_id is not None:
        target_group = await db.get(DeviceGroup, target_group_id)
        if target_group is None:
            raise HTTPException(status_code=404, detail="Group not found")
        touched_group_ids.add(target_group_id)

    for group_id in touched_group_ids:
        if not await can_manage_group_membership(user, db, group_id):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Managing device group membership requires devices:write plus "
                    "schedule read/write access to every affected group"
                ),
            )


def _sort_group_summaries(groups: list[DeviceGroupSummary]) -> list[DeviceGroupSummary]:
    return sorted(groups, key=lambda group: (group.name.lower(), str(group.id)))


async def _load_group_summary_map(
    db: AsyncSession,
    group_ids: set[uuid.UUID],
) -> dict[uuid.UUID, DeviceGroupSummary]:
    if not group_ids:
        return {}
    rows = await db.execute(
        select(DeviceGroup.id, DeviceGroup.name).where(DeviceGroup.id.in_(group_ids))
    )
    return {
        group_id: DeviceGroupSummary(id=group_id, name=group_name)
        for group_id, group_name in rows.all()
    }


async def _require_existing_groups(
    db: AsyncSession,
    group_ids: list[uuid.UUID] | set[uuid.UUID],
) -> dict[uuid.UUID, DeviceGroupSummary]:
    group_id_set = set(group_ids)
    summaries = await _load_group_summary_map(db, group_id_set)
    missing = group_id_set - set(summaries)
    if missing:
        raise HTTPException(status_code=404, detail="Group not found")
    return summaries


async def _load_effective_group_summaries_by_device_id(
    devices: list[Device],
    db: AsyncSession,
) -> dict[str, list[DeviceGroupSummary]]:
    if not devices:
        return {}

    effective_rows = effective_device_group_rows_subquery(
        device_ids=[device.id for device in devices],
    )
    membership_rows = await db.execute(
        select(
            effective_rows.c.device_id,
            effective_rows.c.group_id,
        )
        .select_from(effective_rows)
    )
    group_ids_by_device_id: dict[str, set[uuid.UUID]] = defaultdict(set)
    all_group_ids: set[uuid.UUID] = set()
    for device_id, group_id in membership_rows.all():
        group_ids_by_device_id[device_id].add(group_id)
        all_group_ids.add(group_id)

    group_summary_map = await _load_group_summary_map(db, all_group_ids)
    return {
        device.id: _sort_group_summaries(
            [
                group_summary_map[group_id]
                for group_id in group_ids_by_device_id.get(device.id, set())
                if group_id in group_summary_map
            ]
        )
        for device in devices
    }


def _device_membership_out_kwargs(
    device: Device,
    groups: list[DeviceGroupSummary],
) -> dict:
    primary_group = groups[0] if groups else None
    return {
        "group_id": primary_group.id if primary_group else None,
        "group_name": primary_group.name if primary_group else None,
        "group_ids": [group.id for group in groups],
        "groups": groups,
    }


async def _load_schedule_match_summaries_by_group_id(
    db: AsyncSession,
    group_ids: set[uuid.UUID],
) -> dict[uuid.UUID, list[DeviceScheduleMatchSummary]]:
    if not group_ids:
        return {}
    rows = await db.execute(
        select(
            Schedule.id,
            Schedule.name,
            Schedule.group_id,
            DeviceGroup.name,
        )
        .join(DeviceGroup, DeviceGroup.id == Schedule.group_id)
        .where(
            Schedule.enabled == True,  # noqa: E712
            Schedule.group_id.in_(group_ids),
        )
        .order_by(DeviceGroup.name, Schedule.name, Schedule.id)
    )
    schedule_map: dict[uuid.UUID, list[DeviceScheduleMatchSummary]] = defaultdict(list)
    for schedule_id, schedule_name, group_id, group_name in rows.all():
        schedule_map[group_id].append(
            DeviceScheduleMatchSummary(
                id=schedule_id,
                name=schedule_name,
                group_id=group_id,
                group_name=group_name,
            )
        )
    return schedule_map


async def _build_membership_mutation_response(
    device: Device,
    db: AsyncSession,
    change: DeviceMembershipChange,
    *,
    dry_run: bool,
) -> DeviceGroupMembershipMutationOut:
    summary_map = await _load_group_summary_map(
        db,
        set(change.current_group_ids) | set(change.result_group_ids),
    )
    current_groups = _sort_group_summaries(
        [
            summary_map[group_id]
            for group_id in change.current_group_ids
            if group_id in summary_map
        ]
    )
    result_groups = _sort_group_summaries(
        [
            summary_map[group_id]
            for group_id in change.result_group_ids
            if group_id in summary_map
        ]
    )
    schedule_map = await _load_schedule_match_summaries_by_group_id(
        db,
        set(change.added_group_ids) | set(change.removed_group_ids),
    )
    schedules_added = [
        schedule
        for group_id in change.added_group_ids
        for schedule in schedule_map.get(group_id, [])
    ]
    schedules_removed = [
        schedule
        for group_id in change.removed_group_ids
        for schedule in schedule_map.get(group_id, [])
    ]
    projected_group = summary_map.get(change.legacy_group_id) if change.legacy_group_id else None
    return DeviceGroupMembershipMutationOut(
        device_id=device.id,
        dry_run=dry_run,
        changed=change.changed,
        group_id=change.legacy_group_id,
        group_name=projected_group.name if projected_group else None,
        group_ids=list(change.result_group_ids),
        groups=result_groups,
        current_group_ids=list(change.current_group_ids),
        current_groups=current_groups,
        added_group_ids=list(change.added_group_ids),
        removed_group_ids=list(change.removed_group_ids),
        schedules_added=schedules_added,
        schedules_removed=schedules_removed,
    )


async def _verify_replace_membership_access(
    user,
    device: Device,
    db: AsyncSession,
    target_group_ids: list[uuid.UUID],
) -> None:
    current_group_ids = await get_device_group_ids(device, db)
    touched_group_ids = current_group_ids | set(target_group_ids)
    for group_id in touched_group_ids:
        if not await can_manage_group_membership(user, db, group_id):
            raise HTTPException(
                status_code=403,
                detail=(
                    "Managing device group membership requires devices:write plus "
                    "schedule read/write access to every affected group"
                ),
            )
    await assert_authority_over_group_set(
        user,
        db,
        set(target_group_ids) - current_group_ids,
    )


async def _push_default_asset(device_id: str, asset: Asset, base_url: str, db: AsyncSession) -> None:
    """Send fetch_asset for a default asset, then push a full sync.

    The sync includes the updated default_asset and splash fields, so the
    device evaluator will start playing it once downloaded.  We do NOT send
    a separate play command — that caused a race where the player tried to
    play an asset that hadn't finished downloading yet.
    """
    from cms.routers.ws import _resolve_asset_for_device

    device_q = await db.execute(select(Device).where(Device.id == device_id))
    device = device_q.scalar_one_or_none()
    if not device:
        return

    fetch = await _resolve_asset_for_device(asset, device, base_url, db)
    if fetch:
        await get_transport().send_to_device(device_id, fetch.model_dump(mode="json"))

    # Push a fresh sync so the device learns the new default_asset and splash
    # immediately (instead of waiting up to 15s for the scheduler cycle).
    await push_sync_to_device(device_id, db)


# ── Devices ──


@router.post("/check-updates", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def check_for_updates(db: AsyncSession = Depends(get_db)):
    """Trigger an immediate check for the latest device firmware version."""
    latest_bundle = await check_now(db)
    return {"latest_version": latest_bundle.target_version if latest_bundle else None}


@router.get("", response_model=List[DeviceOut], dependencies=[Depends(require_permission(DEVICES_READ))])
async def list_devices(request: Request, db: AsyncSession = Depends(get_db)):
    from cms.services.scheduler import compute_now_playing
    from cms.auth import SETTING_TIMEZONE, get_setting
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone

    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)
    now = datetime.now(timezone.utc)

    user = getattr(request.state, "user", None)
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None

    # Hide pending/orphaned devices from users without devices:manage
    from cms.permissions import has_permission
    user_perms = user.role.permissions if user and user.role else []
    can_manage = has_permission(user_perms, DEVICES_MANAGE)

    query = select(Device).order_by(Device.registered_at)
    if not can_manage:
        query = query.where(Device.status == DeviceStatus.ADOPTED)
    if not is_admin:
        query = query.where(build_device_read_scope_clause(group_ids))

    result = await db.execute(query)
    devices = result.scalars().all()
    groups_by_device_id = await _load_effective_group_summaries_by_device_id(devices, db)
    _transport = get_transport()
    live_states = {s["device_id"]: s for s in await _transport.get_all_states()}
    connected_ids = set(await _transport.connected_ids())
    scheduled_device_ids = {np["device_id"] for np in await compute_now_playing(db, tz, now)}

    # Build URL→display name map for resolving URL-based asset names
    from shared.models.asset import Asset as AssetModel
    url_assets_q = await db.execute(
        select(AssetModel.url, AssetModel.filename).where(AssetModel.url.isnot(None))
    )
    _url_display = {}
    for url, fname in url_assets_q.all():
        _url_display.setdefault(url, fname)

    def _resolve_asset_name(device_id: str) -> str | None:
        if device_id not in live_states:
            return None
        raw = live_states[device_id]["asset"]
        return _url_display.get(raw, raw) if raw else raw

    from cms.services.bundle_checker import is_os_update_available
    # Issue #578: read the shared latest-version once per request and thread
    # it through the per-device update_available check, so every replica
    # returns the same view of "update available" within a single response.
    # Both channels are read once; each device resolves to its own channel.
    latest_stable = await get_latest_os_version(db, CHANNEL_STABLE)
    latest_prerelease = await get_latest_os_version(db, CHANNEL_PRERELEASE)
    return [
        DeviceOut(
            **_device_row_kwargs(d),
            **_device_membership_out_kwargs(
                d,
                groups_by_device_id.get(d.id, []),
            ),
            is_online=d.id in connected_ids,
            is_upgrading=_is_upgrading(d, now=now),
            upgrade_stuck=_is_upgrade_stuck(d, now=now),
            playback_mode=live_states[d.id]["mode"] if d.id in live_states else None,
            playback_asset=_resolve_asset_name(d.id),
            pipeline_state=live_states[d.id]["pipeline_state"] if d.id in live_states else None,
            display_connected=live_states[d.id]["display_connected"] if d.id in live_states else None,
            display_ports=live_states[d.id]["display_ports"] if d.id in live_states else None,
            cpu_temp_c=live_states[d.id]["cpu_temp_c"] if d.id in live_states else None,
            ip_address=(
                live_states[d.id]["ip_address"]
                if d.id in live_states and live_states[d.id]["ip_address"]
                else d.ip_address
            ),
            ssh_enabled=live_states[d.id]["ssh_enabled"] if d.id in live_states else None,
            local_api_enabled=live_states[d.id]["local_api_enabled"] if d.id in live_states else None,
            error=live_states[d.id]["error"] if d.id in live_states else None,
            uptime_seconds=live_states[d.id]["uptime_seconds"] if d.id in live_states else 0,
            update_available=is_os_update_available(
                d.os_version, _latest_for_device(d, latest_stable, latest_prerelease)
            ),
            has_active_schedule=d.id in scheduled_device_ids,
            **_ota_fields_for_out(d, now=now),
        )
        for d in devices
    ]


@router.get("/{device_id}", response_model=DeviceOut, dependencies=[Depends(require_permission(DEVICES_READ))])
async def get_device(device_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    from cms.services.scheduler import compute_now_playing
    from cms.auth import SETTING_TIMEZONE
    from cms.ui import get_setting
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone

    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)
    now = datetime.now(timezone.utc)

    device = await _get_device_with_access(device_id, request, db)
    groups = (await _load_effective_group_summaries_by_device_id([device], db)).get(device.id, [])
    _transport = get_transport()
    live_states = {s["device_id"]: s for s in await _transport.get_all_states()}
    is_online = await _transport.is_connected(device.id)
    scheduled_device_ids = {np["device_id"] for np in await compute_now_playing(db, tz, now)}

    # Resolve URL-based asset names
    raw_asset = live_states[device.id]["asset"] if device.id in live_states else None
    if raw_asset:
        from shared.models.asset import Asset as AssetModel
        url_q = await db.execute(
            select(AssetModel.filename).where(AssetModel.url == raw_asset).limit(1)
        )
        resolved = url_q.scalar_one_or_none()
        if resolved:
            raw_asset = resolved

    from cms.services.bundle_checker import is_os_update_available
    latest_stable = await get_latest_os_version(db, CHANNEL_STABLE)  # issue #578: shared cross-replica view
    latest_prerelease = await get_latest_os_version(db, CHANNEL_PRERELEASE)
    latest_version = _latest_for_device(device, latest_stable, latest_prerelease)
    return DeviceOut(
        **_device_row_kwargs(device),
        **_device_membership_out_kwargs(device, groups),
        is_online=is_online,
        is_upgrading=_is_upgrading(device, now=now),
        upgrade_stuck=_is_upgrade_stuck(device, now=now),
        playback_mode=live_states[device.id]["mode"] if device.id in live_states else None,
        playback_asset=raw_asset,
        pipeline_state=live_states[device.id]["pipeline_state"] if device.id in live_states else None,
        display_connected=live_states[device.id]["display_connected"] if device.id in live_states else None,
        display_ports=live_states[device.id]["display_ports"] if device.id in live_states else None,
        cpu_temp_c=live_states[device.id]["cpu_temp_c"] if device.id in live_states else None,
        ip_address=(
            live_states[device.id]["ip_address"]
            if device.id in live_states and live_states[device.id]["ip_address"]
            else device.ip_address
        ),
        ssh_enabled=live_states[device.id]["ssh_enabled"] if device.id in live_states else None,
        local_api_enabled=live_states[device.id]["local_api_enabled"] if device.id in live_states else None,
        error=live_states[device.id]["error"] if device.id in live_states else None,
        uptime_seconds=live_states[device.id]["uptime_seconds"] if device.id in live_states else 0,
        update_available=is_os_update_available(device.os_version, latest_version),
        has_active_schedule=device.id in scheduled_device_ids,
        **_ota_fields_for_out(device, now=now),
    )


@router.get(
    "/{device_id}/schedule-status",
    response_model=DeviceScheduleStatusOut,
    dependencies=[Depends(require_permission(DEVICES_READ))],
)
async def get_device_schedule_status_route(
    device_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from cms.auth import SETTING_TIMEZONE
    from cms.ui import get_setting
    from zoneinfo import ZoneInfo
    from datetime import datetime, timezone

    await _get_device_with_access(device_id, request, db)
    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    return await get_device_schedule_status(
        db,
        device_id,
        ZoneInfo(tz_name),
        datetime.now(timezone.utc),
    )


@router.patch("/{device_id}", response_model=DeviceOut, dependencies=[Depends(require_permission(DEVICES_WRITE))])
async def update_device(
    device_id: str,
    data: DeviceUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from cms.permissions import has_permission

    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")

    user = getattr(request.state, "user", None)
    if user:
        await verify_resource_group_access(user, db, await get_device_group_ids(device, db))

    updates = data.model_dump(exclude_unset=True)
    group_ids_in_request = "group_ids" in updates
    requested_group_ids = list(dict.fromkeys(updates.pop("group_ids", []) or []))

    if "group_id" in updates and user:
        await _verify_membership_change_access(
            user,
            device,
            db,
            target_group_id=updates["group_id"],
        )
    if group_ids_in_request:
        await _require_existing_groups(db, requested_group_ids)
        if user:
            await _verify_replace_membership_access(
                user,
                device,
                db,
                requested_group_ids,
            )

    # Validate the requested update channel, if present.
    if "update_channel" in updates and updates["update_channel"] not in CHANNELS:
        raise HTTPException(
            status_code=422,
            detail=f"update_channel must be one of {sorted(CHANNELS)}",
        )

    # Fields that require devices:manage (admin-only)
    managed_fields = {"profile_id", "timezone", "status"}
    restricted = managed_fields & updates.keys()
    if restricted:
        perms = user.role.permissions if user and user.role else []
        if not has_permission(perms, DEVICES_MANAGE):
            raise HTTPException(
                status_code=403,
                detail=f"Missing permission: {DEVICES_MANAGE}",
            )

    # Gate splash assignment on variant readiness (issue #201).
    if updates.get("default_asset_id"):
        await require_asset_ready(db, updates["default_asset_id"])
        # Slideshow defaults require slideshow_v1 capability on the device.
        new_default = await db.get(Asset, updates["default_asset_id"])
        if new_default and new_default.asset_type == AssetType.SLIDESHOW:
            from cms.schemas.protocol import CAPABILITY_SLIDESHOW_V1
            if CAPABILITY_SLIDESHOW_V1 not in (device.capabilities or []):
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Slideshow assets require firmware advertising the "
                        "'slideshow_v1' capability. This device is not compatible."
                    ),
                )

    # Reject assignment to a missing or disabled profile (issue #583).
    # Missing is 404 (a separate pre-existing gap, fixed here because we
    # need the fetch anyway); disabled is 422.
    if updates.get("profile_id"):
        target_profile = await db.get(DeviceProfile, updates["profile_id"])
        if target_profile is None:
            raise HTTPException(status_code=404, detail="Profile not found")
        if not target_profile.enabled:
            raise HTTPException(status_code=422, detail="Profile is disabled")

    # Snapshot before mutation so we can build a true diff for the audit log
    changes = compute_diff(device, updates)
    if group_ids_in_request:
        current_group_ids = sorted(
            (str(group_id) for group_id in await get_device_group_ids(device, db)),
        )
        new_group_ids = sorted(str(group_id) for group_id in requested_group_ids)
        if current_group_ids != new_group_ids:
            changes["group_ids"] = {
                "old": current_group_ids,
                "new": new_group_ids,
            }

    for field, value in updates.items():
        setattr(device, field, value)
    if group_ids_in_request:
        await replace_device_group_memberships(db, device, requested_group_ids)
    elif "group_id" in updates:
        # Mirror into the many-to-many join table (#863, expand/contract window).
        await set_single_group_membership(db, device.id, updates["group_id"])
    await audit_log(
        db, user=user, action="device.update", resource_type="device",
        resource_id=str(device.id),
        description=f"Modified device '{device.name or device.id}'",
        details={"changes": changes},
        request=request,
    )
    await db.commit()
    await db.refresh(device, ["default_asset"])

    # If group_id changed, push a full sync immediately.  The new group
    # may carry schedules that newly target this device (or no longer
    # do), and the inherited group-default asset may change.  Without
    # this push the device would wait up to ~15s for the next scheduler
    # tick to pick up the change.  A full sync covers both schedules and
    # the effective default in one message, so it subsumes the
    # default_asset_id / timezone branches below when group_id/group_ids is also
    # in this PATCH.
    if group_ids_in_request or "group_id" in updates:
        await push_sync_to_device(device_id, db)

    # If default_asset_id was changed, resolve effective default and push
    elif "default_asset_id" in updates:
        from cms.routers.ws import get_asset_base_url
        base_url = get_asset_base_url(request)
        # Resolve: device default → group default → none (splash)
        effective_asset = device.default_asset
        if not effective_asset:
            device_with_groups = (
                await db.execute(
                    select(Device)
                    .options(
                        selectinload(Device.groups).selectinload(
                            DeviceGroup.default_asset
                        )
                    )
                    .where(Device.id == device_id)
                )
            ).scalar_one_or_none()
            if device_with_groups is not None:
                effective_asset = _resolve_group_default_asset(device_with_groups)

        if effective_asset:
            await _push_default_asset(device_id, effective_asset, base_url, db)
        else:
            # No default at any level — push a full sync so the device
            # clears its default_asset and shows splash correctly.
            await push_sync_to_device(device_id, db)

    # If timezone was changed, push a fresh sync so the device applies it
    elif "timezone" in updates:
        await push_sync_to_device(device_id, db)

    groups = (await _load_effective_group_summaries_by_device_id([device], db)).get(device.id, [])
    return DeviceOut(
        **_device_row_kwargs(device),
        **_device_membership_out_kwargs(device, groups),
        is_online=await get_transport().is_connected(device.id),
    )


@router.post(
    "/{device_id}/groups",
    response_model=DeviceGroupMembershipMutationOut,
    dependencies=[Depends(require_permission(DEVICES_WRITE))],
)
async def add_device_group_membership(
    device_id: str,
    body: DeviceGroupAddRequest,
    request: Request,
    dry_run: bool = Query(
        default=False,
        description=(
            "When true, validates authorization and returns the projected "
            "membership + schedule impact without committing or syncing the device."
        ),
    ),
    db: AsyncSession = Depends(get_db),
):
    """Add one group membership to a device.

    The response always describes the resulting membership state. During
    ``dry_run=true`` it is only a preview and no audit log, commit, or sync is
    performed. ``schedules_added``/``schedules_removed`` list enabled schedules
    whose group targeting would newly start or stop matching the device.
    """
    device = await _get_device_with_access(device_id, request, db)
    if await db.get(DeviceGroup, body.group_id) is None:
        raise HTTPException(status_code=404, detail="Group not found")

    user = getattr(request.state, "user", None)
    if user and not await can_manage_group_membership(user, db, body.group_id):
        raise HTTPException(
            status_code=403,
            detail=(
                "Managing device group membership requires devices:write plus "
                "schedule read/write access to the target group"
            ),
        )

    change = await add_device_to_group(db, device, body.group_id, dry_run=dry_run)
    response = await _build_membership_mutation_response(
        device,
        db,
        change,
        dry_run=dry_run,
    )
    if dry_run or not change.changed:
        return response

    await audit_log(
        db,
        user=user,
        action="device.group.add",
        resource_type="device",
        resource_id=str(device.id),
        description=f"Added device '{device.name or device.id}' to a group",
        details={
            "group_id": str(body.group_id),
            "group_ids": [str(group_id) for group_id in change.result_group_ids],
            "added_group_ids": [str(group_id) for group_id in change.added_group_ids],
        },
        request=request,
    )
    await db.commit()
    await push_sync_to_device(device.id, db)
    return await _build_membership_mutation_response(
        device,
        db,
        change,
        dry_run=False,
    )


@router.delete(
    "/{device_id}/groups/{group_id}",
    response_model=DeviceGroupMembershipMutationOut,
    dependencies=[Depends(require_permission(DEVICES_WRITE))],
)
async def remove_device_group_membership(
    device_id: str,
    group_id: uuid.UUID,
    request: Request,
    dry_run: bool = Query(
        default=False,
        description=(
            "When true, validates authorization and returns the projected "
            "membership + schedule impact without committing or syncing the device."
        ),
    ),
    db: AsyncSession = Depends(get_db),
):
    """Remove one group membership from a device.

    Idempotent: removing a group the device does not currently belong to is a
    no-op success. The response always returns the resulting membership state.
    """
    device = await _get_device_with_access(device_id, request, db)
    if await db.get(DeviceGroup, group_id) is None:
        raise HTTPException(status_code=404, detail="Group not found")

    user = getattr(request.state, "user", None)
    if user and not await can_manage_group_membership(user, db, group_id):
        raise HTTPException(
            status_code=403,
            detail=(
                "Managing device group membership requires devices:write plus "
                "schedule read/write access to the target group"
            ),
        )

    change = await remove_device_from_group(db, device, group_id, dry_run=dry_run)
    response = await _build_membership_mutation_response(
        device,
        db,
        change,
        dry_run=dry_run,
    )
    if dry_run or not change.changed:
        return response

    await audit_log(
        db,
        user=user,
        action="device.group.remove",
        resource_type="device",
        resource_id=str(device.id),
        description=f"Removed a group from device '{device.name or device.id}'",
        details={
            "group_id": str(group_id),
            "group_ids": [str(current_group_id) for current_group_id in change.result_group_ids],
            "removed_group_ids": [str(current_group_id) for current_group_id in change.removed_group_ids],
        },
        request=request,
    )
    await db.commit()
    await push_sync_to_device(device.id, db)
    return await _build_membership_mutation_response(
        device,
        db,
        change,
        dry_run=False,
    )


@router.put(
    "/{device_id}/groups",
    response_model=DeviceGroupMembershipMutationOut,
    dependencies=[Depends(require_permission(DEVICES_WRITE))],
)
async def replace_device_group_membership_set(
    device_id: str,
    body: DeviceGroupReplaceRequest,
    request: Request,
    dry_run: bool = Query(
        default=False,
        description=(
            "When true, validates authorization and returns the projected "
            "membership + schedule impact without committing or syncing the device."
        ),
    ),
    db: AsyncSession = Depends(get_db),
):
    """Replace a device's full membership set.

    ``group_ids`` may be empty to fully ungroup the device. The response always
    returns the projected final state, even when ``dry_run=true``.
    """
    device = await _get_device_with_access(device_id, request, db)
    requested_group_ids = list(dict.fromkeys(body.group_ids))
    await _require_existing_groups(db, requested_group_ids)

    user = getattr(request.state, "user", None)
    if user:
        await _verify_replace_membership_access(
            user,
            device,
            db,
            requested_group_ids,
        )

    change = await replace_device_group_memberships(
        db,
        device,
        requested_group_ids,
        dry_run=dry_run,
    )
    response = await _build_membership_mutation_response(
        device,
        db,
        change,
        dry_run=dry_run,
    )
    if dry_run or not change.changed:
        return response

    await audit_log(
        db,
        user=user,
        action="device.group.replace",
        resource_type="device",
        resource_id=str(device.id),
        description=f"Replaced groups for device '{device.name or device.id}'",
        details={
            "group_ids": [str(group_id) for group_id in change.result_group_ids],
            "added_group_ids": [str(group_id) for group_id in change.added_group_ids],
            "removed_group_ids": [str(group_id) for group_id in change.removed_group_ids],
        },
        request=request,
    )
    await db.commit()
    await push_sync_to_device(device.id, db)
    return await _build_membership_mutation_response(
        device,
        db,
        change,
        dry_run=False,
    )


@router.post("/{device_id}/password", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def set_device_password(
    device_id: str,
    body: SetPasswordRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    password = body.password.strip()
    if not password:
        raise HTTPException(status_code=400, detail="Password cannot be empty")
    if len(password) < 4:
        raise HTTPException(status_code=400, detail="Password must be at least 4 characters")

    device = await _get_device_with_access(device_id, request, db)

    if not await get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    config_msg = ConfigMessage(web_password=password)
    sent = await get_transport().send_to_device(device_id, config_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.set_password", resource_type="device",
        resource_id=str(device_id),
        description=f"Reset web password on device '{device.name or device_id}'",
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/reboot", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def reboot_device(
    device_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_with_access(device_id, request, db)

    if not await get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    reboot_msg = RebootMessage()
    sent = await get_transport().send_to_device(device_id, reboot_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.reboot", resource_type="device",
        resource_id=str(device_id),
        description=f"Rebooted device '{device.name or device_id}'",
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/upgrade", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def upgrade_device(
    device_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_with_access(device_id, request, db)

    if not await get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    # Issue agora-cms#626 -- if the device is stuck mid-tryboot, the
    # on-device os-updater FSM is in ``state=tryboot_running`` and will
    # silently drop every dispatch we send.  Refuse the upgrade here
    # *before* taking a claim so we don't lock the row for 15 minutes
    # behind a dispatch the device will never act on.  The UI ought to
    # already be greying out the Update kebab off the ``upgrade_stuck``
    # flag in ``DeviceOut``; this is the server-side belt-and-braces
    # for stale UI / API clients / etc.
    if _is_upgrade_stuck(device):
        raise HTTPException(
            status_code=409,
            detail=(
                "Device is stuck mid-upgrade (tryboot did not complete). "
                "Wait for the device to recover, or reboot it manually, "
                "before retrying."
            ),
        )

    # Stage 4: atomic claim — set ``upgrade_started_at`` iff it's NULL
    # or older than the TTL.  The timestamp we just wrote is captured
    # via RETURNING so a later failure can compare-and-clear without
    # stomping a successor's claim.
    #
    # IMPORTANT: claim BEFORE bundle/version checks so that a held claim
    # always returns 409 ``upgrade_in_progress`` rather than 503
    # ``bundle_not_yet_cached`` or 409 ``already_on_target_version``.
    # The multireplica claim-visibility invariant
    # (test_active_upgrade_claim_visible_across_replicas) depends on
    # this ordering: when a claim is held, both replicas must reject
    # with 409, regardless of bundle cache state.
    claim_ts = datetime.now(timezone.utc)
    ttl_cutoff = claim_ts - UPGRADE_TTL
    result = await db.execute(
        update(Device)
        .where(Device.id == device_id)
        .where(
            or_(
                Device.upgrade_started_at.is_(None),
                Device.upgrade_started_at < ttl_cutoff,
            )
        )
        .where(
            # Issue agora-cms#511: also gate on the send-failure cooldown.
            # A row that just rolled back from a 502 will have this column
            # set ~10s into the future; the CAS must reject the retry
            # until the cooldown elapses.
            or_(
                Device.upgrade_cooldown_until.is_(None),
                Device.upgrade_cooldown_until < claim_ts,
            )
        )
        .values(upgrade_started_at=claim_ts)
        .returning(Device.upgrade_started_at)
        .execution_options(synchronize_session=False)
    )
    claimed = result.scalar_one_or_none()
    await db.commit()
    if claimed is None:
        # Another request holds a live claim within TTL, or we're inside
        # the post-502 send-failure cooldown window.
        raise HTTPException(
            status_code=409,
            detail="Upgrade already in progress for this device",
        )

    async def _release_claim() -> None:
        """Release the claim we just took. Compare-and-clear uses the
        captured ``claimed`` timestamp as the claim token so we don't
        stomp a successor's claim if anyone reclaimed after TTL.
        """
        await db.execute(
            update(Device)
            .where(Device.id == device_id)
            .where(Device.upgrade_started_at == claimed)
            .values(upgrade_started_at=None)
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    async def _release_with_cooldown() -> None:
        """Release the claim AND arm the send-failure cooldown.

        Used by the 502 (send-to-device failed) path only.  We clear
        ``upgrade_started_at`` (so ``_is_upgrading()`` returns False
        and the UI badge stops showing "Upgrading...") AND set
        ``upgrade_cooldown_until = now + SEND_FAILURE_COOLDOWN`` in a
        single UPDATE, guarded by the claim token so we don't disturb
        a successor's claim if one snuck in past the cooldown clause.
        """
        cooldown_until = datetime.now(timezone.utc) + SEND_FAILURE_COOLDOWN
        await db.execute(
            update(Device)
            .where(Device.id == device_id)
            .where(Device.upgrade_started_at == claimed)
            .values(
                upgrade_started_at=None,
                upgrade_cooldown_until=cooldown_until,
            )
            .execution_options(synchronize_session=False)
        )
        await db.commit()

    # M5: read the cached agora-os bundle metadata for the device's channel.
    # The bundle_checker poller refreshes this every 30 min; on a cold CMS
    # start it may still be None for up to one poll interval. Surface that
    # as a retryable 503 rather than dispatching a malformed message.
    latest_bundle = await get_latest_bundle(db, device.update_channel)
    if latest_bundle is None:
        await _release_claim()
        raise HTTPException(
            status_code=503,
            detail="bundle_not_yet_cached",
        )

    # M5: idempotency guard. If the device is already reporting the
    # target version, don't churn a dispatch — the device would just
    # no-op it after download/signature work. This relies on the
    # device having registered with ``os_version`` populated (M4); a
    # device that never reported one (NULL os_version) falls through
    # to dispatch.
    if device.os_version and device.os_version == latest_bundle.target_version:
        await _release_claim()
        raise HTTPException(
            status_code=409,
            detail="already_on_target_version",
        )

    upgrade_msg = OSUpdateDispatchMessage(
        release_id=latest_bundle.release_id,
        target_version=latest_bundle.target_version,
        min_from_version=latest_bundle.min_from_version,
        bundle_url=latest_bundle.bundle_url,
        signature_url=latest_bundle.signature_url,
    )
    sent = await get_transport().send_to_device(device_id, upgrade_msg.model_dump(mode="json"))
    if not sent:
        # Issue agora-cms#511: hold the claim slot for a short cooldown
        # window before allowing another attempt. Without this, a
        # double-click during a slow send returns back-to-back 502s.
        await _release_with_cooldown()
        raise HTTPException(status_code=502, detail="Failed to send to device")

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.upgrade", resource_type="device",
        resource_id=str(device_id),
        description=(
            f"Dispatched os_update_dispatch to device "
            f"'{device.name or device_id}' "
            f"(release_id={latest_bundle.release_id}, "
            f"target_version={latest_bundle.target_version})"
        ),
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/ssh", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def toggle_device_ssh(
    device_id: str,
    body: ToggleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    enabled = body.enabled
    device = await _get_device_with_access(device_id, request, db)

    if not await get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    config_msg = ConfigMessage(ssh_enabled=enabled)
    sent = await get_transport().send_to_device(device_id, config_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    # Track the SSH state immediately so the UI reflects it
    await get_transport().set_state_flags(device_id, ssh_enabled=enabled)

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.ssh_toggle", resource_type="device",
        resource_id=str(device_id),
        description=f"{'Enabled' if enabled else 'Disabled'} SSH on device '{device.name or device_id}'",
        details={"enabled": enabled},
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/factory-reset", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def factory_reset_device(
    device_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    device = await _get_device_with_access(device_id, request, db)

    if not await get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    msg = FactoryResetMessage()
    sent = await get_transport().send_to_device(device_id, msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.factory_reset", resource_type="device",
        resource_id=str(device_id),
        description=f"Triggered factory reset on device '{device.name or device_id}'",
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/local-api", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def toggle_device_local_api(
    device_id: str,
    body: ToggleRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    enabled = body.enabled
    device = await _get_device_with_access(device_id, request, db)

    if not await get_transport().is_connected(device_id):
        raise HTTPException(status_code=409, detail="Device is not connected")

    config_msg = ConfigMessage(local_api_enabled=enabled)
    sent = await get_transport().send_to_device(device_id, config_msg.model_dump(mode="json"))
    if not sent:
        raise HTTPException(status_code=502, detail="Failed to send to device")

    await get_transport().set_state_flags(device_id, local_api_enabled=enabled)

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.local_api_toggle", resource_type="device",
        resource_id=str(device_id),
        description=f"{'Enabled' if enabled else 'Disabled'} local API on device '{device.name or device_id}'",
        details={"enabled": enabled},
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/{device_id}/adopt", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def adopt_device(device_id: str, body: AdoptRequest, request: Request, db: AsyncSession = Depends(get_db)):
    """Adopt a pending device or re-adopt an orphaned one.

    For pending devices: sets status to adopted and assigns an auth token on next connect.
    For orphaned devices: clears stored auth credentials so a new token is assigned on reconnect.

    Optionally accepts a JSON body with name, location, and either deprecated
    ``group_id`` or additive ``group_ids`` to configure the device during
    adoption.

    In both cases, a wipe_assets command is sent so the device starts fresh
    without stale content from a previous adoption.

    Accepts optional name and group assignment to configure the device during
    adoption.
    """
    device = await _get_device_with_access(device_id, request, db)

    if device.status == DeviceStatus.PENDING:
        device.status = DeviceStatus.ADOPTED
    elif device.status == DeviceStatus.ORPHANED:
        device.device_auth_token_hash = None
        device.device_api_key_hash = None
        device.previous_api_key_hash = None
        device.api_key_rotated_at = None
        device.status = DeviceStatus.ADOPTED
    else:
        raise HTTPException(status_code=400, detail="Device is already adopted")

    # Apply optional name, location, and group assignment
    if body.name is not None:
        device.name = body.name
    if body.location is not None:
        device.location = body.location
    requested_group_ids = list(dict.fromkeys(body.group_ids or [])) if body.group_ids is not None else None
    if body.group_id is not None:
        user = getattr(request.state, "user", None)
        if user is not None:
            await _verify_membership_change_access(
                user,
                device,
                db,
                target_group_id=body.group_id,
            )
    if requested_group_ids is not None:
        await _require_existing_groups(db, requested_group_ids)
        user = getattr(request.state, "user", None)
        if user is not None:
            await _verify_replace_membership_access(
                user,
                device,
                db,
                requested_group_ids,
            )
    if body.group_id is not None:
        device.group_id = body.group_id
        # Mirror into the many-to-many join table (#863, expand/contract window).
        await set_single_group_membership(db, device.id, body.group_id)
    elif requested_group_ids is not None:
        await replace_device_group_memberships(db, device, requested_group_ids)

    # Verify and assign the encoder profile (required).
    # Reject if missing (404) or disabled (422) — issue #583.
    target_profile = await db.get(DeviceProfile, body.profile_id)
    if target_profile is None:
        raise HTTPException(status_code=404, detail="Profile not found")
    if not target_profile.enabled:
        raise HTTPException(status_code=422, detail="Profile is disabled")
    device.profile_id = body.profile_id

    await db.commit()

    # Tell the device to wipe cached assets so it starts clean
    wipe_msg = WipeAssetsMessage(reason="adopted")
    await get_transport().send_to_device(device_id, wipe_msg.model_dump(mode="json"))

    # Push a fresh sync so the device learns its new status immediately
    # (e.g. the OOBE screen advances from "waiting for adoption" to "adopted").
    await push_sync_to_device(device_id, db)

    groups = (await _load_effective_group_summaries_by_device_id([device], db)).get(device.id, [])
    primary_group = groups[0] if groups else None

    desc = f"Adopted device '{device.name or device_id}'"
    if primary_group:
        desc += f" into group '{primary_group.name}'"
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.adopt", resource_type="device",
        resource_id=str(device_id),
        description=desc,
        details={
            "name": device.name,
            "location": device.location,
            "group_id": str(primary_group.id) if primary_group else None,
            "group_ids": [str(group.id) for group in groups],
            "profile_id": str(device.profile_id) if device.profile_id else None,
        },
        request=request,
    )
    await db.commit()

    return {"ok": True}


@router.delete("/{device_id}", dependencies=[Depends(require_permission(DEVICES_MANAGE))])
async def delete_device(device_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    device = await _get_device_with_access(device_id, request, db)
    device_name = device.name

    # Tell the device to wipe cached assets before we remove it from the DB
    wipe_msg = WipeAssetsMessage(reason="deleted")
    await get_transport().send_to_device(device_id, wipe_msg.model_dump(mode="json"))

    # Remove referencing rows before deleting the device
    from cms.models.asset import DeviceAsset

    await db.execute(
        DeviceAsset.__table__.delete().where(DeviceAsset.device_id == device_id)
    )

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="device.delete", resource_type="device",
        resource_id=str(device_id),
        description=f"Deleted device '{device_name or device_id}'",
        details={"name": device_name},
        request=request,
    )
    await db.delete(device)
    await db.commit()
    return {"deleted": device_id}


# ── Groups ──


@router.get("/groups/", response_model=List[DeviceGroupOut], dependencies=[Depends(require_permission(GROUPS_READ))])
async def list_groups(request: Request, db: AsyncSession = Depends(get_db)):
    user = getattr(request.state, "user", None)
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None

    effective_rows = effective_device_group_rows_subquery()
    query = (
        select(
            DeviceGroup,
            func.count(func.distinct(effective_rows.c.device_id)).label("device_count"),
        )
        .outerjoin(effective_rows, effective_rows.c.group_id == DeviceGroup.id)
        .group_by(DeviceGroup.id)
        .order_by(DeviceGroup.name)
    )
    if not is_admin:
        if group_ids:
            query = query.where(DeviceGroup.id.in_(group_ids))
        else:
            query = query.where(False)

    result = await db.execute(query)
    return [
        DeviceGroupOut(
            id=group.id,
            name=group.name,
            description=group.description,
            default_asset_id=group.default_asset_id,
            device_count=count,
            created_at=group.created_at,
        )
        for group, count in result.all()
    ]


@router.post("/groups/", response_model=DeviceGroupOut, status_code=201, dependencies=[Depends(require_permission(GROUPS_WRITE))])
async def create_group(data: DeviceGroupCreate, request: Request, db: AsyncSession = Depends(get_db)):
    # Gate splash assignment on variant readiness (issue #201).
    if data.default_asset_id:
        await require_asset_ready(db, data.default_asset_id)

    group = DeviceGroup(name=data.name, description=data.description, default_asset_id=data.default_asset_id)
    db.add(group)
    try:
        await db.flush()
    except IntegrityError:
        # Unique constraint on ``device_groups.name`` — surface as 409
        # so callers (UI, e2e tests, scripts) can distinguish a
        # duplicate from a real server error.  Race-safe: we let the
        # DB enforce uniqueness rather than do a TOCTOU pre-check.
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail=f"Device group named '{data.name}' already exists",
        )

    # Auto-add non-admin creator to the new group. Without this, the list_groups
    # endpoint (which filters by user_groups for non-admins) would hide the
    # group from its own creator — it looks to the user as if creation failed.
    # Admins are view_all and don't need an explicit membership row.
    user = getattr(request.state, "user", None)
    if user is not None:
        user_group_ids = await get_user_group_ids(user, db)
        if user_group_ids is not None:  # None => admin / view_all, skip
            from cms.models.user import UserGroup
            db.add(UserGroup(user_id=user.id, group_id=group.id))
            await db.flush()

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="group.create", resource_type="group",
        resource_id=str(group.id),
        description=f"Created device group '{group.name}'",
        details={
            "name": group.name,
            "description": group.description,
            "default_asset_id": str(group.default_asset_id) if group.default_asset_id else None,
        },
        request=request,
    )
    await db.commit()
    await db.refresh(group)
    return DeviceGroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        default_asset_id=group.default_asset_id,
        device_count=0,
        created_at=group.created_at,
    )


@router.patch("/groups/{group_id}", response_model=DeviceGroupOut, dependencies=[Depends(require_permission(GROUPS_WRITE))])
async def update_group(
    group_id: uuid.UUID,
    data: DeviceGroupUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(DeviceGroup).where(DeviceGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    user = getattr(request.state, "user", None)
    if user:
        await verify_resource_group_access(user, db, group_id)

    updates = data.model_dump(exclude_unset=True)
    changes = compute_diff(group, updates)

    # Gate splash assignment on variant readiness (issue #201).
    if updates.get("default_asset_id"):
        await require_asset_ready(db, updates["default_asset_id"])
        # Slideshow group default requires every adopted member to advertise
        # slideshow_v1 — same precedent as the schedule create/update gate.
        new_default = await db.get(Asset, updates["default_asset_id"])
        if new_default and new_default.asset_type == AssetType.SLIDESHOW:
            from cms.schemas.protocol import CAPABILITY_SLIDESHOW_V1
            effective_rows = effective_device_group_rows_subquery(
                group_ids=[group_id],
                statuses=DeviceStatus.ADOPTED,
            )
            members_q = await db.execute(
                select(Device)
                .join(effective_rows, effective_rows.c.device_id == Device.id)
            )
            incompatible = [
                d for d in members_q.scalars().all()
                if CAPABILITY_SLIDESHOW_V1 not in (d.capabilities or [])
            ]
            if incompatible:
                names = ", ".join(d.name or d.id for d in incompatible[:3])
                suffix = (
                    f" and {len(incompatible) - 3} more"
                    if len(incompatible) > 3 else ""
                )
                raise HTTPException(
                    status_code=422,
                    detail=(
                        "Slideshow assets require firmware advertising the "
                        "'slideshow_v1' capability. These devices in the "
                        f"group are not compatible: {names}{suffix}"
                    ),
                )

    for field, value in updates.items():
        setattr(group, field, value)
    await audit_log(
        db, user=user,
        action="group.update", resource_type="group",
        resource_id=str(group_id),
        description=f"Modified device group '{group.name}'",
        details={"changes": changes},
        request=request,
    )
    await db.commit()
    await db.refresh(group, ["default_asset"])

    # When default_asset_id changes, push an immediate sync to all group
    # members so they pick up the new asset without waiting for the next
    # scheduler cycle (~15s).  Mirrors the per-device handler behaviour.
    if "default_asset_id" in updates:
        from cms.routers.ws import get_asset_base_url

        base_url = get_asset_base_url(request)
        effective_rows = effective_device_group_rows_subquery(
            group_ids=[group.id],
        )
        devices_q = await db.execute(
            select(Device)
            .options(selectinload(Device.default_asset))
            .join(effective_rows, effective_rows.c.device_id == Device.id)
        )
        for device in devices_q.scalars().all():
            # Resolve: device default → group default → none (splash)
            effective_asset = device.default_asset
            if not effective_asset:
                effective_asset = group.default_asset

            if effective_asset:
                await _push_default_asset(device.id, effective_asset, base_url, db)
            else:
                await push_sync_to_device(device.id, db)

    effective_rows = effective_device_group_rows_subquery(
        group_ids=[group.id],
    )
    count_q = await db.execute(
        select(func.count(func.distinct(effective_rows.c.device_id)))
        .select_from(effective_rows)
    )
    return DeviceGroupOut(
        id=group.id,
        name=group.name,
        description=group.description,
        default_asset_id=group.default_asset_id,
        device_count=count_q.scalar() or 0,
        created_at=group.created_at,
    )


@router.delete("/groups/{group_id}", dependencies=[Depends(require_permission(GROUPS_WRITE))])
async def delete_group(group_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    from cms.models.schedule import Schedule

    result = await db.execute(select(DeviceGroup).where(DeviceGroup.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    user = getattr(request.state, "user", None)
    if user:
        await verify_resource_group_access(user, db, group_id)

    # Block deletion if any schedule references this group
    sched_count = await db.scalar(
        select(func.count()).select_from(Schedule).where(Schedule.group_id == group_id)
    )
    if sched_count:
        raise HTTPException(
            status_code=409,
            detail=f"Cannot delete — group is used by {sched_count} schedule(s). Remove it from all schedules first.",
        )

    effective_rows = effective_device_group_rows_subquery(
        group_ids=[group_id],
    )
    affected_membership_rows = await db.execute(
        select(effective_rows.c.device_id).select_from(effective_rows)
    )
    affected_device_ids = {
        *affected_membership_rows.scalars().all(),
    }

    await audit_log(
        db, user=user,
        action="group.delete", resource_type="group",
        resource_id=str(group_id),
        description=f"Deleted device group '{group.name}'",
        details={"name": group.name},
        request=request,
    )
    await db.delete(group)
    await db.commit()
    for device_id in sorted(affected_device_ids):
        await push_sync_to_device(device_id, db)
    return {"deleted": str(group_id)}


@router.get("/groups/{group_id}/panel", dependencies=[Depends(require_permission(GROUPS_READ))])
async def get_group_panel(group_id: uuid.UUID, request: Request, db: AsyncSession = Depends(get_db)):
    """Return the rendered <div class='group-panel'> HTML for one group.

    Mirrors the pattern established by GET /api/assets/{id}/row and
    /api/profiles/{id}/row so the cross-session poller on /devices and the
    createGroup handler can insert server-rendered markup instead of
    synthesizing HTML in JS or issuing a full page reload. See issue #87.
    """
    from fastapi.responses import HTMLResponse
    from cms.ui import templates
    from cms.services.variant_view import is_asset_ready as _is_asset_ready
    from cms.models.schedule import Schedule as ScheduleModel

    user = getattr(request.state, "user", None)
    if user:
        await verify_resource_group_access(user, db, group_id)

    result = await db.execute(
        select(DeviceGroup).where(DeviceGroup.id == group_id)
    )
    group = result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    user_perms = list(user.role.permissions) if user and user.role else []
    can_manage = DEVICES_MANAGE in user_perms

    device_query = select(Device).order_by(Device.name, Device.id)
    if not can_manage:
        device_query = device_query.where(Device.status == DeviceStatus.ADOPTED)
    if user:
        visible_group_ids = await get_user_group_ids(user, db)
        if visible_group_ids is not None:
            device_query = device_query.where(build_device_read_scope_clause(visible_group_ids))
    device_rows = (await db.execute(device_query)).scalars().all()
    groups_by_device_id = await _load_effective_group_summaries_by_device_id(device_rows, db)
    group.panel_devices = sorted(
        [
            device
            for device in device_rows
            if group_id in {summary.id for summary in groups_by_device_id.get(device.id, [])}
        ],
        key=lambda device: ((device.name or device.id).lower(), device.id),
    )

    # Annotate the same fields the /devices page template expects.
    group.device_count = len(group.panel_devices)
    group.schedule_count = await db.scalar(
        select(func.count()).select_from(ScheduleModel).where(ScheduleModel.group_id == group_id)
    ) or 0

    from cms.services.bundle_checker import is_os_update_available
    latest_stable = await get_latest_os_version(db, CHANNEL_STABLE)  # issue #578: shared cross-replica view
    latest_prerelease = await get_latest_os_version(db, CHANNEL_PRERELEASE)
    latest_version = latest_stable  # template fallback; per-device value set below
    transport = get_transport()
    live_states = {s["device_id"]: s for s in await transport.get_all_states()}
    for d in group.panel_devices:
        # See cms/ui.py: detach before decorating with live-state attributes
        # so display-only values (cpu_temp_c, ip_address, …) cannot autoflush
        # back to the DB.  Some of these names collide with real columns.
        db.expunge(d)
        d.is_online = await transport.is_connected(d.id)
        state = live_states.get(d.id)
        d.cpu_temp_c = state["cpu_temp_c"] if state else None
        # #436: prefer live LAN IP, fall back to last-known persisted value.
        live_ip = state["ip_address"] if state else None
        d.ip_address = live_ip or d.ip_address
        d.playback_mode = state["mode"] if state else None
        d.playback_asset = state["asset"] if state else None
        d.pipeline_state = state["pipeline_state"] if state else None
        d.started_at = state["started_at"] if state else None
        d.playback_position_ms = state["playback_position_ms"] if state else None
        d.ssh_enabled = state["ssh_enabled"] if state else None
        d.local_api_enabled = state["local_api_enabled"] if state else None
        d.available_version = _latest_for_device(d, latest_stable, latest_prerelease)
        d.update_available = is_os_update_available(d.os_version, d.available_version)
        d.is_upgrading = _is_upgrading(d)
        d.has_active_schedule = False  # poller will flip this via updateLiveFields
        d.ui_groups = groups_by_device_id.get(d.id, [])
        d.ui_group_ids = [summary.id for summary in d.ui_groups]

    # Splash-screen dropdown options need the same ready annotations ui.py
    # applies on the full page render.
    assets_q = await db.execute(
        select(Asset)
        .options(selectinload(Asset.variants))
        .where(Asset.deleted_at.is_(None))
        .order_by(Asset.filename)
    )
    assets = assets_q.scalars().all()
    for a in assets:
        ready, reason = _is_asset_ready(a.variants)
        a.ready_for_selection = ready
        a.not_ready_reason = reason

    # All groups the user can see — populates each device row's group-select.
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None
    if is_admin:
        visible_groups = (await db.execute(
            select(DeviceGroup).order_by(DeviceGroup.name)
        )).scalars().all()
    elif group_ids:
        visible_groups = (await db.execute(
            select(DeviceGroup).where(DeviceGroup.id.in_(group_ids)).order_by(DeviceGroup.name)
        )).scalars().all()
    else:
        visible_groups = []

    pending_ttl_hours = get_settings().pending_device_ttl_hours

    # Phase C: rich device_row needs profiles, latest_version, timezones, and
    # per-device severity_tags + per-group rollup.
    from cms.models.device_profile import DeviceProfile
    from cms.services.device_alerts import device_severity_tags, fleet_counts
    from cms.ui import COMMON_TIMEZONES

    profiles_q = await db.execute(
        select(DeviceProfile)
        .where(DeviceProfile.purpose == "device")
        .order_by(DeviceProfile.name)
    )
    profiles = profiles_q.scalars().all()
    # Reuse the latest_version we read above for the per-device update_available
    # decoration — this is the same shared cross-replica value.
    timezones = COMMON_TIMEZONES

    for d in group.panel_devices:
        d.severity_tags = device_severity_tags(d, user_perms)
    group.rollup = fleet_counts(group.panel_devices, user_perms)

    macros = templates.env.get_template("_macros.html").module
    html = macros.group_panel(
        group, user_perms, assets, visible_groups, pending_ttl_hours,
        profiles, latest_version, timezones,
    )
    return HTMLResponse(str(html))


# ── Device-originated: WPS connect token ────────────────────────────
#
# A device authenticates with its API key and asks the CMS to mint a
# WPS client access token.  Only available when DEVICE_TRANSPORT=wps;
# returns 404 on the direct-WS deployment so devices discover the mode
# automatically.


def _hash_device_api_key(key: str) -> str:
    import hashlib
    return hashlib.sha256(key.encode()).hexdigest()


async def _authenticate_device(
    device_id: str, api_key: str | None, db: AsyncSession,
) -> Device:
    """Verify the presented X-Device-API-Key matches `device_id`.

    Returns the Device on success, raises 401/404 otherwise.  Accepts
    the previous key within the standard rotation grace window.
    """
    from datetime import datetime, timedelta, timezone as _tz

    if not api_key:
        raise HTTPException(status_code=401, detail="X-Device-API-Key required")

    result = await db.execute(select(Device).where(Device.id == device_id))
    device = result.scalar_one_or_none()
    if device is None:
        raise HTTPException(status_code=404, detail="Device not found")

    key_hash = _hash_device_api_key(api_key)
    if device.device_api_key_hash == key_hash:
        return device
    if (
        device.previous_api_key_hash == key_hash
        and device.api_key_rotated_at is not None
    ):
        rotated_at = device.api_key_rotated_at
        if rotated_at.tzinfo is None:
            rotated_at = rotated_at.replace(tzinfo=_tz.utc)
        if datetime.now(_tz.utc) - rotated_at < timedelta(seconds=300):
            return device
    raise HTTPException(status_code=401, detail="Invalid device API key")


@device_originated_router.post("/{device_id}/connect-token")
async def connect_token(
    device_id: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Mint a WPS client access token (URL + JWT) for `device_id`.

    Auth: `X-Device-API-Key` header bound to `device_id`.
    Behaviour depends on the configured transport:
      - `DEVICE_TRANSPORT=wps`: return {url, token} from the WPS SDK.
      - else: 404 so devices flip back to the direct-WS path.
    """
    settings = get_settings()
    if settings.device_transport != "wps":
        raise HTTPException(status_code=404, detail="WPS transport not enabled")

    api_key = request.headers.get("X-Device-API-Key")
    await _authenticate_device(device_id, api_key, db)

    transport = get_transport()
    if not hasattr(transport, "get_client_access_token"):
        raise HTTPException(
            status_code=500, detail="Transport does not support client tokens",
        )
    minutes = getattr(settings, "wps_token_lifetime_minutes", 60)
    token = await transport.get_client_access_token(device_id, minutes_to_expire=minutes)
    return {
        "url": token.get("url"),
        "token": token.get("token"),
    }