"""Web UI routes — Jinja2 server-rendered pages."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import (
    COOKIE_NAME,
    MAX_AGE,
    SETTING_PASSWORD_HASH,
    SETTING_TIMEZONE,
    SETTING_USERNAME,
    get_setting,
    get_settings,
    hash_password,
    require_auth,
    set_setting,
    verify_password,
)
from cms.config import Settings
from cms.database import get_db
from cms.models.asset import Asset, AssetVariant, VariantStatus
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.device_profile import DeviceProfile
from cms.models.schedule import Schedule
from cms.services.device_manager import device_manager
from cms.services.version_checker import get_latest_device_version, is_update_available

import json as _json
from zoneinfo import ZoneInfo, available_timezones

templates = Jinja2Templates(directory="cms/templates")

# Custom Jinja2 filter for days of week
def select_days(day_names, day_numbers):
    return ", ".join(day_names[i - 1] for i in sorted(day_numbers) if 1 <= i <= 7)

templates.env.filters["select_days"] = select_days


def schedule_json(s):
    """Serialize a schedule ORM object to a JSON string for the edit modal."""
    def _12h(t):
        h = t.hour
        period = "AM" if h < 12 else "PM"
        if h == 0:
            h = 12
        elif h > 12:
            h -= 12
        return {"hour": h, "minute": t.minute, "period": period}
    st = _12h(s.start_time)
    et = _12h(s.end_time)
    data = {
        "id": str(s.id),
        "name": s.name,
        "asset_id": str(s.asset_id),
        "device_id": s.device_id,
        "group_id": str(s.group_id) if s.group_id else None,
        "start_hour": st["hour"],
        "start_minute": st["minute"],
        "start_period": st["period"],
        "end_hour": et["hour"],
        "end_minute": et["minute"],
        "end_period": et["period"],
        "start_date": s.start_date.strftime("%Y-%m-%d") if s.start_date else "",
        "end_date": s.end_date.strftime("%Y-%m-%d") if s.end_date else "",
        "days_of_week": s.days_of_week or [],
        "priority": s.priority,
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
    username = form.get("username", "")
    password = form.get("password", "")

    stored_username = await get_setting(db, SETTING_USERNAME) or settings.admin_username
    stored_hash = await get_setting(db, SETTING_PASSWORD_HASH)

    # Verify credentials
    valid = False
    if username == stored_username:
        if stored_hash:
            valid = verify_password(password, stored_hash)
        else:
            # Fallback to env var if DB not seeded yet
            valid = password == settings.admin_password

    if valid:
        serializer = URLSafeTimedSerializer(settings.secret_key)
        token = serializer.dumps({"user": username})
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
        d.is_online = False

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

    # Now Playing
    now_playing = get_now_playing()

    # Build device status with live playback state
    live_states = {s["device_id"]: s for s in device_manager.get_all_states()}
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
    upcoming = get_upcoming_schedules(all_schedules, now, tz)
    upcoming_today = [u for u in upcoming if u["day_label"] == "today"]
    upcoming_tomorrow = [u for u in upcoming if u["day_label"] == "tomorrow"]

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
    device_states = [
        {
            "id": did,
            "mode": live_states[did]["mode"] if did in live_states else "offline",
            "asset": live_states[did]["asset"] if did in live_states else None,
            "cpu_temp_c": live_states[did]["cpu_temp_c"] if did in live_states else None,
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
    upcoming = get_upcoming_schedules(all_schedules, now, tz)

    return JSONResponse({
        "now_playing": now_playing,
        "pending_ids": pending_ids,
        "orphaned_ids": orphaned_ids,
        "offline_ids": offline_ids,
        "device_states": device_states,
        "upcoming": upcoming,
    })


# ── Devices ──


@router.get("/devices", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def devices_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Device).options(selectinload(Device.group)).order_by(Device.name, Device.id)
    )
    devices = result.scalars().all()
    live_states = {s["device_id"]: s for s in device_manager.get_all_states()}
    for d in devices:
        d.is_online = device_manager.is_connected(d.id)
        state = live_states.get(d.id)
        d.cpu_temp_c = state["cpu_temp_c"] if state else None
        d.ip_address = state["ip_address"] if state else None
        d.update_available = is_update_available(d.firmware_version)

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
            d.update_available = is_update_available(d.firmware_version)

    # Devices not assigned to any group
    ungrouped = [d for d in devices if d.group_id is None and d.status != DeviceStatus.PENDING]

    assets_q = await db.execute(select(Asset).order_by(Asset.filename))
    assets = assets_q.scalars().all()

    return templates.TemplateResponse(request, "devices.html", {
        "active_tab": "devices",
        "devices": devices,
        "groups": groups,
        "ungrouped": ungrouped,
        "assets": assets,
        "latest_version": get_latest_device_version(),
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
    schedules = schedules_q.scalars().all()

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

    from datetime import datetime, timezone as _tz
    now_utc = datetime.now(_tz.utc)
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
        "schedules": schedules,
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
        "active_tab": "profiles",
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

    return templates.TemplateResponse(request, "settings.html", {
        "active_tab": "settings",
        "version": __version__,
        "username": username,
        "online_count": device_manager.connected_count,
        "asset_storage": str(settings.asset_storage_path),
        "success": None,
        "error": None,
    })


@router.post("/settings/password", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def change_password(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    from cms import __version__

    form = await request.form()
    current_password = form.get("current_password", "")
    new_password = form.get("new_password", "")
    confirm_password = form.get("confirm_password", "")

    username = await get_setting(db, SETTING_USERNAME) or settings.admin_username
    ctx = {
        "active_tab": "settings",
        "version": __version__,
        "username": username,
        "online_count": device_manager.connected_count,
        "asset_storage": str(settings.asset_storage_path),
        "success": None,
        "error": None,
    }

    # Validate current password
    stored_hash = await get_setting(db, SETTING_PASSWORD_HASH)
    if stored_hash:
        if not verify_password(current_password, stored_hash):
            ctx["error"] = "Current password is incorrect"
            return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)
    else:
        if current_password != settings.admin_password:
            ctx["error"] = "Current password is incorrect"
            return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)

    # Validate new password
    if len(new_password) < 6:
        ctx["error"] = "New password must be at least 6 characters"
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)

    if new_password != confirm_password:
        ctx["error"] = "New passwords do not match"
        return templates.TemplateResponse(request, "settings.html", ctx, status_code=400)

    # Save new hash
    await set_setting(db, SETTING_PASSWORD_HASH, hash_password(new_password))

    ctx["success"] = "Password updated successfully"
    return templates.TemplateResponse(request, "settings.html", ctx)


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
