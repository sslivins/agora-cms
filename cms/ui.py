"""Web UI routes — Jinja2 server-rendered pages."""

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import (
    COOKIE_NAME,
    MAX_AGE,
    SETTING_MCP_API_KEY,
    SETTING_MCP_ENABLED,
    SETTING_PASSWORD_HASH,
    SETTING_TIMEZONE,
    SETTING_USERNAME,
    _resolve_user_from_session,
    get_current_user,
    get_setting,
    get_settings,
    hash_password,
    require_auth,
    require_permission,
    set_setting,
    verify_password,
)
from cms.config import Settings
from cms.database import get_db
from cms.models.asset import Asset, AssetVariant, VariantStatus
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.permissions import USERS_READ, USERS_WRITE, ROLES_WRITE
from cms.models.device_profile import DeviceProfile
from cms.models.schedule import Schedule
from cms.models.schedule_log import ScheduleLog, ScheduleLogEvent
from cms.models.user import User
from cms.services.device_manager import device_manager
from cms.services.version_checker import get_latest_device_version, is_update_available
from cms.routers.devices import _upgrading as _devices_upgrading

import json as _json
from datetime import datetime, timezone as _tz
from zoneinfo import ZoneInfo, available_timezones

# Common timezones for the device timezone dropdown — sorted for readability.
# Uses well-known IANA zone names; excludes deprecated / obscure entries.
COMMON_TIMEZONES = sorted([
    tz for tz in available_timezones()
    if tz.startswith(("Africa/", "America/", "Asia/", "Atlantic/", "Australia/",
                      "Europe/", "Indian/", "Pacific/"))
    and not tz.startswith(("America/Argentina/", "America/Indiana/",
                           "America/Kentucky/", "America/North_Dakota/"))
] + ["Etc/UTC"])

templates = Jinja2Templates(directory="cms/templates")

# Custom Jinja2 filter for days of week
def select_days(day_names, day_numbers):
    return ", ".join(day_names[i - 1] for i in sorted(day_numbers) if 1 <= i <= 7)

templates.env.filters["select_days"] = select_days


def schedule_json(s):
    """Serialize a schedule ORM object to a JSON string for the edit modal."""
    data = {
        "id": str(s.id),
        "name": s.name,
        "asset_id": str(s.asset_id),
        "device_id": s.device_id,
        "group_id": str(s.group_id) if s.group_id else None,
        "start_time": s.start_time.strftime("%H:%M"),
        "end_time": s.end_time.strftime("%H:%M"),
        "start_date": s.start_date.strftime("%Y-%m-%d") if s.start_date else "",
        "end_date": s.end_date.strftime("%Y-%m-%d") if s.end_date else "",
        "days_of_week": s.days_of_week or [],
        "priority": s.priority,
        "enabled": s.enabled,
        "loop_count": s.loop_count,
    }
    return _json.dumps(data)

templates.env.filters["schedule_json"] = schedule_json

router = APIRouter()


# ── Auth ──


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    login_id = form.get("email", "") or form.get("username", "")
    password = form.get("password", "")

    # Authenticate against the users table — try email first, then username for backward compat
    from sqlalchemy import select as sa_select, or_
    result = await db.execute(
        sa_select(User).where(
            or_(User.email == login_id, User.username == login_id),
            User.is_active.is_(True),
        )
    )
    user = result.scalar_one_or_none()

    valid = False
    if user is not None:
        valid = verify_password(password, user.password_hash)

    if valid:
        # Update last_login_at
        from datetime import datetime, timezone
        user.last_login_at = datetime.now(timezone.utc)
        await db.commit()

        serializer = URLSafeTimedSerializer(settings.secret_key)
        token = serializer.dumps({"user_id": str(user.id)})

        # Check if user must change password on first login
        if user.must_change_password:
            response = RedirectResponse(url="/force-password-change", status_code=303)
        else:
            response = RedirectResponse(url="/", status_code=303)

        response.set_cookie(
            COOKIE_NAME, token, max_age=MAX_AGE, httponly=True, samesite="lax"
        )
        return response

    return templates.TemplateResponse(
        request, "login.html", {"error": "Invalid credentials"}, status_code=401
    )


@router.get("/logout")
async def logout():
    response = RedirectResponse(url="/login", status_code=303)
    response.delete_cookie(COOKIE_NAME)
    return response


# ── Force password change ──


@router.get("/force-password-change", response_class=HTMLResponse)
async def force_password_change_page(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Show the forced password change page for first-login users."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return RedirectResponse(url="/login", status_code=303)
    user = await _resolve_user_from_session(cookie, settings, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)
    if not user.must_change_password:
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(request, "force_password_change.html", {"error": None})


@router.post("/force-password-change")
async def force_password_change_submit(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Handle forced password change submission."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return RedirectResponse(url="/login", status_code=303)
    user = await _resolve_user_from_session(cookie, settings, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    form = await request.form()
    new_password = form.get("new_password", "")
    confirm_password = form.get("confirm_password", "")

    if len(new_password) < 6:
        return templates.TemplateResponse(
            request, "force_password_change.html",
            {"error": "Password must be at least 6 characters"}, status_code=400
        )
    if new_password != confirm_password:
        return templates.TemplateResponse(
            request, "force_password_change.html",
            {"error": "Passwords do not match"}, status_code=400
        )

    user.password_hash = hash_password(new_password)
    user.must_change_password = False
    await db.commit()

    return RedirectResponse(url="/", status_code=303)


# ── Dashboard ──


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    from datetime import datetime as _dt, timezone as _tz
    from cms.services.scheduler import get_now_playing, get_upcoming_schedules

    # CMS timezone
    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)
    now = _dt.now(_tz.utc)

    # Pending devices
    pending_q = await db.execute(
        select(Device).where(Device.status == DeviceStatus.PENDING).order_by(Device.registered_at)
    )
    pending_devices = pending_q.scalars().all()
    for d in pending_devices:
        d.is_online = device_manager.is_connected(d.id)

    # All adopted devices
    devices_q = await db.execute(
        select(Device)
        .options(selectinload(Device.group))
        .where(Device.status == DeviceStatus.ADOPTED)
        .order_by(Device.name, Device.id)
    )
    all_devices = devices_q.scalars().all()
    for d in all_devices:
        d.is_online = device_manager.is_connected(d.id)

    # Offline devices (adopted but not connected)
    offline_devices = [d for d in all_devices if not d.is_online]

    # Orphaned devices
    orphaned_q = await db.execute(
        select(Device)
        .options(selectinload(Device.group))
        .where(Device.status == DeviceStatus.ORPHANED)
        .order_by(Device.name, Device.id)
    )
    orphaned_devices = orphaned_q.scalars().all()

    # Now Playing — enrich with actual device state for mismatch detection
    now_playing = get_now_playing()
    live_states = {s["device_id"]: s for s in device_manager.get_all_states()}
    for np in now_playing:
        did = np["device_id"]
        live = live_states.get(did, {})
        actual_mode = live.get("mode", "unknown")
        actual_asset = live.get("asset")
        expected_asset = np.get("asset_filename")
        # Mismatch: schedule says play this asset, but device isn't playing it
        np["actual_mode"] = actual_mode
        np["actual_asset"] = actual_asset
        np["mismatch"] = actual_mode != "play" or actual_asset != expected_asset
        # Grace period: suppress mismatch for ~45s after schedule activation
        # (device needs time to receive sync, evaluate, and start playback)
        if np["mismatch"] and np.get("since"):
            since = _dt.fromisoformat(np["since"])
            if (now - since).total_seconds() < 45:
                np["mismatch"] = False
                # Only show "starting" if the device isn't playing yet.
                # If it's playing a different asset (e.g. higher-priority
                # schedule preempted but _now_playing is stale), don't
                # flip the badge — the device is working fine.
                if actual_mode != "play":
                    np["starting"] = True

    # Build device status with live playback state
    device_states = []
    for d in all_devices:
        state = live_states.get(d.id)
        device_states.append({
            "id": d.id,
            "name": d.name or d.id,
            "group_name": d.group.name if d.group else None,
            "is_online": d.is_online,
            "mode": state["mode"] if state else "offline",
            "asset": state["asset"] if state else None,
            "cpu_temp_c": state["cpu_temp_c"] if state else None,
            "error": state["error"] if state else None,
            "error_since": state["error_since"] if state else None,
            "display_connected": state["display_connected"] if state else None,
        })

    # Upcoming schedules (next 24h)
    sched_q = await db.execute(
        select(Schedule)
        .options(
            selectinload(Schedule.asset),
            selectinload(Schedule.device),
            selectinload(Schedule.group),
        )
        .where(Schedule.enabled == True)
    )
    all_schedules = sched_q.scalars().all()
    upcoming = get_upcoming_schedules(all_schedules, now, tz, now_playing=now_playing)
    upcoming_today = [u for u in upcoming if u["day_label"] == "today"]
    upcoming_tomorrow = [u for u in upcoming if u["day_label"] == "tomorrow"]

    # Recent activity (last 24h)
    from datetime import timedelta
    cutoff_24h = now - timedelta(hours=24)
    recent_q = await db.execute(
        select(ScheduleLog)
        .where(ScheduleLog.timestamp >= cutoff_24h)
        .order_by(ScheduleLog.timestamp.desc())
        .limit(10)
    )
    recent_activity = recent_q.scalars().all()

    return templates.TemplateResponse(request, "dashboard.html", {
        "active_tab": "dashboard",
        "tz": tz,
        "pending_devices": pending_devices,
        "orphaned_devices": orphaned_devices,
        "offline_devices": offline_devices,
        "all_devices": all_devices,
        "now_playing": now_playing,
        "device_states": device_states,
        "upcoming_today": upcoming_today,
        "upcoming_tomorrow": upcoming_tomorrow,
        "recent_activity": recent_activity,
    })


@router.get("/api/server-time", dependencies=[Depends(require_auth)])
async def server_time_json(db: AsyncSession = Depends(get_db)):
    """Return the CMS server's configured timezone and current local time."""
    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)
    now_utc = datetime.now(_tz.utc)
    now_local = now_utc.astimezone(tz)
    return JSONResponse({
        "timezone": tz_name,
        "utc": now_utc.isoformat(),
        "local": now_local.isoformat(),
    })


@router.get("/api/dashboard", dependencies=[Depends(require_auth)])
async def dashboard_json(db: AsyncSession = Depends(get_db)):
    """Lightweight JSON endpoint for dashboard polling."""
    from datetime import datetime as _dt, timezone as _tz
    from cms.services.scheduler import get_now_playing, get_upcoming_schedules

    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)
    now = _dt.now(_tz.utc)

    # Pending device IDs
    pending_q = await db.execute(
        select(Device.id).where(Device.status == DeviceStatus.PENDING)
    )
    pending_ids = [r[0] for r in pending_q.all()]

    # Orphaned device IDs
    orphaned_q = await db.execute(
        select(Device.id).where(Device.status == DeviceStatus.ORPHANED)
    )
    orphaned_ids = [r[0] for r in orphaned_q.all()]

    # Online status (adopted devices only)
    devices_q = await db.execute(
        select(Device.id)
        .where(Device.status == DeviceStatus.ADOPTED)
    )
    all_device_ids = [r[0] for r in devices_q.all()]
    offline_ids = [did for did in all_device_ids if not device_manager.is_connected(did)]

    now_playing = get_now_playing()

    live_states = {s["device_id"]: s for s in device_manager.get_all_states()}
    for np in now_playing:
        did = np["device_id"]
        live = live_states.get(did, {})
        actual_mode = live.get("mode", "unknown")
        actual_asset = live.get("asset")
        np["mismatch"] = actual_mode != "play" or actual_asset != np.get("asset_filename")
        if np["mismatch"] and np.get("since"):
            since = _dt.fromisoformat(np["since"])
            if (now - since).total_seconds() < 45:
                np["mismatch"] = False
                if actual_mode != "play":
                    np["starting"] = True

    device_states = [
        {
            "id": did,
            "mode": live_states[did]["mode"] if did in live_states else "offline",
            "asset": live_states[did]["asset"] if did in live_states else None,
            "cpu_temp_c": live_states[did]["cpu_temp_c"] if did in live_states else None,
            "error": live_states[did]["error"] if did in live_states else None,
            "display_connected": live_states[did]["display_connected"] if did in live_states else None,
        }
        for did in all_device_ids
    ]

    # Upcoming schedules
    sched_q = await db.execute(
        select(Schedule)
        .options(
            selectinload(Schedule.asset),
            selectinload(Schedule.device),
            selectinload(Schedule.group),
        )
        .where(Schedule.enabled == True)
    )
    all_schedules = sched_q.scalars().all()
    upcoming = get_upcoming_schedules(all_schedules, now, tz, now_playing=now_playing)

    # Recent activity count for change detection
    from datetime import timedelta
    cutoff_24h = now - timedelta(hours=24)
    activity_count_q = await db.execute(
        select(func.count(ScheduleLog.id)).where(ScheduleLog.timestamp >= cutoff_24h)
    )
    activity_count = activity_count_q.scalar() or 0

    return JSONResponse({
        "now_playing": now_playing,
        "pending_ids": pending_ids,
        "orphaned_ids": orphaned_ids,
        "offline_ids": offline_ids,
        "device_states": device_states,
        "upcoming": upcoming,
        "activity_count": activity_count,
    })


# ── Devices ──


@router.get("/devices", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def devices_page(request: Request, db: AsyncSession = Depends(get_db)):
    from cms.services.scheduler import get_now_playing

    result = await db.execute(
        select(Device).options(selectinload(Device.group)).order_by(Device.name, Device.id)
    )
    devices = result.scalars().all()
    live_states = {s["device_id"]: s for s in device_manager.get_all_states()}
    scheduled_device_ids = {np["device_id"] for np in get_now_playing()}
    for d in devices:
        d.is_online = device_manager.is_connected(d.id)
        state = live_states.get(d.id)
        d.cpu_temp_c = state["cpu_temp_c"] if state else None
        d.ip_address = state["ip_address"] if state else None
        d.playback_mode = state["mode"] if state else None
        d.playback_asset = state["asset"] if state else None
        d.pipeline_state = state["pipeline_state"] if state else None
        d.started_at = state["started_at"] if state else None
        d.playback_position_ms = state["playback_position_ms"] if state else None
        d.ssh_enabled = state["ssh_enabled"] if state else None
        d.local_api_enabled = state["local_api_enabled"] if state else None
        d.update_available = is_update_available(d.firmware_version)
        d.is_upgrading = d.id in _devices_upgrading
        d.has_active_schedule = d.id in scheduled_device_ids

    groups_q = await db.execute(
        select(DeviceGroup)
        .options(selectinload(DeviceGroup.devices))
        .order_by(DeviceGroup.name)
    )
    groups = groups_q.scalars().all()

    # Attach device_count and is_online to each group's devices
    for g in groups:
        g.device_count = len(g.devices)
        for d in g.devices:
            d.is_online = device_manager.is_connected(d.id)
            state = live_states.get(d.id)
            d.cpu_temp_c = state["cpu_temp_c"] if state else None
            d.ip_address = state["ip_address"] if state else None
            d.playback_mode = state["mode"] if state else None
            d.playback_asset = state["asset"] if state else None
            d.pipeline_state = state["pipeline_state"] if state else None
            d.started_at = state["started_at"] if state else None
            d.playback_position_ms = state["playback_position_ms"] if state else None
            d.ssh_enabled = state["ssh_enabled"] if state else None
            d.local_api_enabled = state["local_api_enabled"] if state else None
            d.update_available = is_update_available(d.firmware_version)
            d.is_upgrading = d.id in _devices_upgrading
            d.has_active_schedule = d.id in scheduled_device_ids

    # Devices not assigned to any group
    ungrouped = [d for d in devices if d.group_id is None and d.status != DeviceStatus.PENDING]

    assets_q = await db.execute(select(Asset).order_by(Asset.filename))
    assets = assets_q.scalars().all()

    profiles_q = await db.execute(select(DeviceProfile).order_by(DeviceProfile.name))
    profiles = profiles_q.scalars().all()

    return templates.TemplateResponse(request, "devices.html", {
        "active_tab": "devices",
        "devices": devices,
        "groups": groups,
        "ungrouped": ungrouped,
        "assets": assets,
        "profiles": profiles,
        "timezones": COMMON_TIMEZONES,
        "latest_version": get_latest_device_version(),
        "pending_ttl_hours": get_settings().pending_device_ttl_hours,
    })


# ── Users & Roles ──


@router.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    user: User = Depends(require_permission(USERS_READ)),
    db: AsyncSession = Depends(get_db),
):
    request.state.user = user

    # All users with roles eager-loaded
    users_q = await db.execute(
        select(User).options(selectinload(User.role)).order_by(User.email)
    )
    users = users_q.scalars().all()

    # Load group memberships for each user
    from cms.models.user import UserGroup
    ug_q = await db.execute(select(UserGroup))
    all_ug = ug_q.all()
    user_groups_map: dict = {}
    for ug in all_ug:
        user_groups_map.setdefault(str(ug.user_id), []).append(str(ug.group_id))

    # All roles
    from cms.models.user import Role
    roles_q = await db.execute(select(Role).order_by(Role.name))
    roles = roles_q.scalars().all()

    # All device groups (for assigning users to groups)
    groups_q = await db.execute(select(DeviceGroup).order_by(DeviceGroup.name))
    groups = groups_q.scalars().all()

    # All permissions for the role editor
    from cms.permissions import ALL_PERMISSIONS, PERMISSION_DESCRIPTIONS
    all_permissions = ALL_PERMISSIONS
    perm_descriptions = PERMISSION_DESCRIPTIONS

    # Check if current user can write
    can_write = USERS_WRITE in (user.role.permissions if user.role else [])
    can_write_roles = ROLES_WRITE in (user.role.permissions if user.role else [])

    return templates.TemplateResponse(request, "users.html", {
        "active_tab": "users",
        "users": users,
        "user_groups_map": user_groups_map,
        "roles": roles,
        "groups": groups,
        "all_permissions": all_permissions,
        "perm_descriptions": perm_descriptions,
        "can_write": can_write,
        "can_write_roles": can_write_roles,
        "current_user_id": str(user.id),
    })


# ── Assets ──


@router.get("/assets", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def assets_page(request: Request, db: AsyncSession = Depends(get_db)):

    result = await db.execute(
        select(Asset)
        .options(selectinload(Asset.variants).selectinload(AssetVariant.profile))
        .order_by(Asset.uploaded_at.desc())
    )
    assets = result.scalars().all()

    # Count schedules per asset
    sched_counts_q = await db.execute(
        select(Schedule.asset_id, func.count()).group_by(Schedule.asset_id)
    )
    sched_counts = dict(sched_counts_q.all())

    # Annotate each asset with variant summary + schedule count
    for a in assets:
        total = len(a.variants)
        ready = sum(1 for v in a.variants if v.status == VariantStatus.READY)
        processing = sum(1 for v in a.variants if v.status == VariantStatus.PROCESSING)
        failed = sum(1 for v in a.variants if v.status == VariantStatus.FAILED)
        a.variant_total = total
        a.variant_ready = ready
        a.variant_processing = processing
        a.variant_failed = failed
        a.schedule_count = sched_counts.get(a.id, 0)

    return templates.TemplateResponse(request, "assets.html", {
        "active_tab": "assets",
        "assets": assets,
    })





# ── Schedules ──


@router.get("/schedules", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def schedules_page(request: Request, db: AsyncSession = Depends(get_db)):
    schedules_q = await db.execute(
        select(Schedule)
        .options(
            selectinload(Schedule.asset),
            selectinload(Schedule.device),
            selectinload(Schedule.group),
        )
        .order_by(Schedule.priority.desc(), Schedule.name)
    )
    all_schedules = schedules_q.scalars().all()

    assets_q = await db.execute(select(Asset).order_by(Asset.filename))
    assets = assets_q.scalars().all()

    devices_q = await db.execute(
        select(Device).where(Device.status == DeviceStatus.ADOPTED).order_by(Device.name)
    )
    devices = devices_q.scalars().all()

    groups_q = await db.execute(select(DeviceGroup).order_by(DeviceGroup.name))
    groups = groups_q.scalars().all()

    current_timezone = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    timezone_saved = (await get_setting(db, SETTING_TIMEZONE)) is not None

    now_utc = datetime.now(_tz.utc)
    local_now = now_utc.astimezone(ZoneInfo(current_timezone))
    today_local = local_now.date()
    local_now_time = local_now.time()

    active_schedules = []
    expired_schedules = []
    for s in all_schedules:
        if s.end_date:
            # Use the calendar date (UTC date portion) — this matches the date
            # the user picked in the date picker, which is stored as midnight
            # UTC.  Converting to local time would shift the date backwards for
            # timezones behind UTC, causing a false-expired off-by-one.
            # This is consistent with _matches_now() and get_upcoming_schedules().
            end_cal = s.end_date.date()
            if end_cal < today_local:
                expired_schedules.append(s)
                continue
            # Same-day: expired if time window already closed (non-overnight only)
            if end_cal == today_local and s.start_time < s.end_time and s.end_time <= local_now_time:
                expired_schedules.append(s)
                continue
        active_schedules.append(s)

    tz_options = []
    for tz_name in sorted(available_timezones()):
        offset = now_utc.astimezone(ZoneInfo(tz_name)).utcoffset()
        total_sec = int(offset.total_seconds())
        sign = "+" if total_sec >= 0 else "-"
        h, m = divmod(abs(total_sec) // 60, 60)
        label = f"{tz_name.replace('_', ' ')} (UTC{sign}{h:02d}:{m:02d})"
        tz_options.append({"value": tz_name, "label": label})

    return templates.TemplateResponse(request, "schedules.html", {
        "active_tab": "schedules",
        "schedules": active_schedules,
        "expired_schedules": expired_schedules,
        "assets": assets,
        "devices": devices,
        "groups": groups,
        "current_timezone": current_timezone,
        "timezone_saved": timezone_saved,
        "tz_options": tz_options,
    })


# ── Profiles ──


@router.get("/profiles", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def profiles_page(request: Request, db: AsyncSession = Depends(get_db)):

    result = await db.execute(
        select(DeviceProfile).order_by(DeviceProfile.name)
    )
    profiles = result.scalars().all()

    # Annotate each profile with device/variant counts
    annotated = []
    for p in profiles:
        dev_count = (await db.execute(
            select(func.count(Device.id)).where(Device.profile_id == p.id)
        )).scalar() or 0
        total_var = (await db.execute(
            select(func.count(AssetVariant.id)).where(AssetVariant.profile_id == p.id)
        )).scalar() or 0
        ready_var = (await db.execute(
            select(func.count(AssetVariant.id)).where(
                AssetVariant.profile_id == p.id,
                AssetVariant.status == VariantStatus.READY,
            )
        )).scalar() or 0

        p.device_count = dev_count
        p.total_variants = total_var
        p.ready_variants = ready_var
        annotated.append(p)

    # Active transcoding queue
    queue_result = await db.execute(
        select(AssetVariant)
        .where(AssetVariant.status.in_([VariantStatus.PENDING, VariantStatus.PROCESSING, VariantStatus.FAILED]))
        .order_by(AssetVariant.created_at)
        .limit(50)
    )
    queue_variants = queue_result.scalars().all()

    # Load relationships for display
    transcode_queue = []
    for v in queue_variants:
        await db.refresh(v, ["source_asset", "profile"])
        v.source_filename = v.source_asset.filename if v.source_asset else "?"
        v.profile_name = v.profile.name if v.profile else "?"
        transcode_queue.append(v)

    return templates.TemplateResponse(request, "profiles.html", {
        "active_tab": "assets",
        "profiles": annotated,
        "transcode_queue": transcode_queue,
    })


# ── Settings ──


@router.get("/settings", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from cms import __version__

    username = await get_setting(db, SETTING_USERNAME) or settings.admin_username
    mcp_enabled = (await get_setting(db, SETTING_MCP_ENABLED)) == "true"
    mcp_api_key = await get_setting(db, SETTING_MCP_API_KEY) or ""

    # Device list for log download panel
    from cms.models.device import Device
    result = await db.execute(select(Device).order_by(Device.name))
    devices = result.scalars().all()
    device_list = [
        {"id": d.id, "name": d.name, "connected": device_manager.is_connected(d.id)}
        for d in devices
    ]

    return templates.TemplateResponse(request, "settings.html", {
        "active_tab": "settings",
        "version": __version__,
        "username": username,
        "online_count": device_manager.connected_count,
        "asset_storage": str(settings.asset_storage_path),
        "mcp_enabled": mcp_enabled,
        "mcp_api_key": mcp_api_key,
        "devices": device_list,
        "success": None,
        "error": None,
    })


@router.post("/settings/password", response_class=HTMLResponse)
async def change_password(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
    current_user: User = Depends(get_current_user),
):
    from cms import __version__

    form = await request.form()
    current_password = form.get("current_password", "")
    new_password = form.get("new_password", "")
    confirm_password = form.get("confirm_password", "")

    ctx = {
        "active_tab": "settings",
        "version": __version__,
        "username": current_user.email,
        "online_count": device_manager.connected_count,
        "asset_storage": str(settings.asset_storage_path),
        "success": None,
        "error": None,
    }

    # Validate current password against user record
    if not verify_password(current_password, current_user.password_hash):
        ctx["error"] = "Current password is incorrect"
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)

    # Validate new password
    if len(new_password) < 6:
        ctx["error"] = "New password must be at least 6 characters"
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)

    if new_password != confirm_password:
        ctx["error"] = "New passwords do not match"
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)

    # Update password on the User row and keep cms_settings in sync
    current_user.password_hash = hash_password(new_password)
    await db.commit()
    await set_setting(db, SETTING_PASSWORD_HASH, current_user.password_hash)

    ctx["success"] = "Password updated successfully"
    return templates.TemplateResponse(request, "settings.html", ctx)


# ── MCP Settings (JSON API used by settings page JS) ──


@router.get("/api/mcp/status", dependencies=[Depends(require_auth)])
async def mcp_health_check():
    """Check if the MCP container is reachable and can talk to the CMS API."""
    import httpx
    from cms.auth import get_settings
    mcp_url = get_settings().mcp_server_url.rstrip("/")
    result = {"online": False, "api_connected": False}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{mcp_url}/health")
            result["online"] = resp.status_code == 200
            if result["online"]:
                api_resp = await client.get(f"{mcp_url}/health/api")
                if api_resp.status_code == 200:
                    data = api_resp.json()
                    result["api_connected"] = data.get("status") == "ok"
                else:
                    result["api_error"] = api_resp.json().get("detail", "API check failed")
    except Exception as exc:
        result["error"] = str(exc)
    return result


@router.post("/api/mcp/toggle", dependencies=[Depends(require_auth)])
async def mcp_toggle(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Enable or disable the MCP server."""
    body = await request.json()
    enabled = body.get("enabled", False)
    await set_setting(db, SETTING_MCP_ENABLED, "true" if enabled else "false")
    return {"enabled": enabled}


@router.post("/api/mcp/generate-key", dependencies=[Depends(require_auth)])
async def mcp_generate_key(db: AsyncSession = Depends(get_db)):
    """Generate a new MCP API key (replaces any existing key)."""
    import secrets
    key = secrets.token_urlsafe(32)
    await set_setting(db, SETTING_MCP_API_KEY, key)
    return {"key": key}


# ── NTP Status ──


@router.get("/api/ntp/status", dependencies=[Depends(require_auth)])
async def ntp_status():
    """Check NTP container health via a raw NTP query."""
    import socket
    import struct
    import time

    try:
        # Build minimal NTP request (v3 client, mode 3)
        msg = b"\x1b" + 47 * b"\0"
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.settimeout(3.0)
        sock.sendto(msg, ("ntp", 123))
        data, _ = sock.recvfrom(48)
        sock.close()

        if len(data) < 48:
            return {"online": False}

        unpacked = struct.unpack("!BBBb11I", data)
        stratum = unpacked[1]
        # Transmit timestamp (seconds since 1900-01-01)
        ntp_time = unpacked[10] + unpacked[11] / 2**32
        # NTP epoch offset from Unix epoch
        ntp_epoch = 2208988800
        server_time = ntp_time - ntp_epoch
        offset = server_time - time.time()

        return {
            "online": True,
            "stratum": stratum,
            "offset_ms": round(offset * 1000, 1),
        }
    except Exception:
        return {"online": False}


# ── Database Status ──


@router.get("/api/db/status", dependencies=[Depends(require_auth)])
async def db_status(db: AsyncSession = Depends(get_db)):
    """Check PostgreSQL health and return key metrics."""
    from sqlalchemy import text

    try:
        # Version
        row = await db.execute(text("SELECT version()"))
        full_version = row.scalar()
        # Extract just "PostgreSQL X.Y"
        version_short = full_version.split(",")[0] if full_version else "unknown"

        # Database size
        row = await db.execute(text(
            "SELECT pg_size_pretty(pg_database_size(current_database()))"
        ))
        db_size = row.scalar()

        # Active connections
        row = await db.execute(text(
            "SELECT count(*) FROM pg_stat_activity WHERE datname = current_database()"
        ))
        connections = row.scalar()

        # Uptime
        row = await db.execute(text("SELECT pg_postmaster_start_time()"))
        start_time = row.scalar()
        uptime_str = None
        if start_time:
            from datetime import datetime, timezone as _tz
            now = datetime.now(_tz.utc)
            if start_time.tzinfo is None:
                start_time = start_time.replace(tzinfo=_tz.utc)
            delta = now - start_time
            days = delta.days
            hours, rem = divmod(delta.seconds, 3600)
            minutes = rem // 60
            parts = []
            if days:
                parts.append(f"{days}d")
            if hours:
                parts.append(f"{hours}h")
            parts.append(f"{minutes}m")
            uptime_str = " ".join(parts)

        # Current user
        row = await db.execute(text("SELECT current_user"))
        db_user = row.scalar()

        return {
            "online": True,
            "version": version_short,
            "size": db_size,
            "connections": connections,
            "uptime": uptime_str,
            "user": db_user,
        }
    except Exception:
        return {"online": False}


@router.post("/api/db/change-password", dependencies=[Depends(require_auth)])
async def db_change_password(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """Change the PostgreSQL user password and reinitialize the connection."""
    from sqlalchemy import text
    from urllib.parse import urlparse, urlunparse
    from cms.database import init_db, dispose_db, _engine

    body = await request.json()
    new_password = body.get("password", "").strip()

    if len(new_password) < 6:
        return JSONResponse(
            {"detail": "Password must be at least 6 characters"},
            status_code=400,
        )

    try:
        # Get current user
        row = await db.execute(text("SELECT current_user"))
        db_user = row.scalar()

        # Change password in PostgreSQL (parameterized via format to avoid
        # SQL injection — password is validated above and we use ALTER USER)
        await db.execute(text(
            f"ALTER USER {db_user} PASSWORD :pwd"
        ), {"pwd": new_password})
        await db.commit()

        # Rebuild engine with new password
        current_url = str(_engine.url)
        parsed = urlparse(current_url)
        new_url = urlunparse(parsed._replace(
            netloc=f"{parsed.username}:{new_password}@{parsed.hostname}:{parsed.port}"
        ))
        await dispose_db()
        # Create a temporary settings-like object with the new URL
        settings = get_settings()
        settings_dict = settings.model_dump()
        settings_dict["database_url"] = new_url
        temp_settings = Settings(**settings_dict)
        init_db(temp_settings)

        return {
            "success": True,
            "warning": "Database password changed. Update POSTGRES_PASSWORD and "
                       "AGORA_CMS_DATABASE_URL in your .env file, then restart "
                       "for the change to persist.",
        }
    except Exception as e:
        return JSONResponse(
            {"detail": f"Failed to change password: {e}"},
            status_code=500,
        )


@router.post("/settings/timezone", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def change_timezone(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from cms import __version__

    form = await request.form()
    tz_name = form.get("timezone", "").strip()

    username = await get_setting(db, SETTING_USERNAME) or settings.admin_username
    timezones = sorted(available_timezones())

    if tz_name not in available_timezones():
        current_timezone = await get_setting(db, SETTING_TIMEZONE) or "UTC"
        return templates.TemplateResponse(request, "settings.html", {
            "active_tab": "settings",
            "version": __version__,
            "username": username,
            "online_count": device_manager.connected_count,
            "asset_storage": str(settings.asset_storage_path),
            "current_timezone": current_timezone,
            "timezone_saved": True,
            "timezones": timezones,
            "success": None,
            "error": f"Invalid timezone: {tz_name}",
        }, status_code=400)

    await set_setting(db, SETTING_TIMEZONE, tz_name)

    return templates.TemplateResponse(request, "settings.html", {
        "active_tab": "settings",
        "version": __version__,
        "username": username,
        "online_count": device_manager.connected_count,
        "asset_storage": str(settings.asset_storage_path),
        "current_timezone": tz_name,
        "timezone_saved": True,
        "timezones": timezones,
        "success": f"Timezone set to {tz_name}",
        "error": None,
    })


# ── History ──


@router.get("/history", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def history_page(
    request: Request,
    page: int = Query(1, ge=1),
    db: AsyncSession = Depends(get_db),
):
    from datetime import datetime as _dt, timezone as _tz

    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)

    per_page = 50
    offset = (page - 1) * per_page

    count_result = await db.execute(select(func.count()).select_from(ScheduleLog))
    total = count_result.scalar()
    total_pages = max(1, (total + per_page - 1) // per_page)

    result = await db.execute(
        select(ScheduleLog)
        .order_by(ScheduleLog.timestamp.desc())
        .limit(per_page)
        .offset(offset)
    )
    logs = result.scalars().all()

    return templates.TemplateResponse(request, "history.html", {
        "active_tab": "history",
        "tz": tz,
        "logs": logs,
        "page": page,
        "total_pages": total_pages,
        "total": total,
    })
