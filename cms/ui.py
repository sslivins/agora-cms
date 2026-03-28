"""Web UI routes — Jinja2 server-rendered pages."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
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
from cms.models.asset import Asset
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.schedule import Schedule
from cms.services.device_manager import device_manager

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
    # Counts
    device_result = await db.execute(select(func.count(Device.id)))
    device_count = device_result.scalar() or 0

    pending_result = await db.execute(
        select(func.count(Device.id)).where(Device.status == DeviceStatus.PENDING)
    )
    pending_count = pending_result.scalar() or 0

    asset_result = await db.execute(select(func.count(Asset.id)))
    asset_count = asset_result.scalar() or 0

    schedule_result = await db.execute(
        select(func.count(Schedule.id)).where(Schedule.enabled == True)
    )
    schedule_count = schedule_result.scalar() or 0

    online_count = device_manager.connected_count

    # Pending devices
    pending_q = await db.execute(
        select(Device).where(Device.status == DeviceStatus.PENDING).order_by(Device.registered_at)
    )
    pending_devices = pending_q.scalars().all()

    # Recent devices
    recent_q = await db.execute(
        select(Device)
        .options(selectinload(Device.group))
        .where(Device.status != DeviceStatus.PENDING)
        .order_by(Device.last_seen.desc().nullslast())
        .limit(10)
    )
    recent_devices = recent_q.scalars().all()

    # Tag online devices
    for d in recent_devices:
        d.is_online = device_manager.is_connected(d.id)
    for d in pending_devices:
        d.is_online = False

    # Now Playing from scheduler
    from cms.services.scheduler import get_now_playing
    now_playing = get_now_playing()

    return templates.TemplateResponse(request, "dashboard.html", {
        "active_tab": "dashboard",
        "device_count": device_count,
        "online_count": online_count,
        "pending_count": pending_count,
        "asset_count": asset_count,
        "schedule_count": schedule_count,
        "pending_devices": pending_devices,
        "recent_devices": recent_devices,
        "now_playing": now_playing,
    })


# ── Devices ──


@router.get("/devices", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def devices_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(Device).options(selectinload(Device.group)).order_by(Device.name, Device.id)
    )
    devices = result.scalars().all()
    for d in devices:
        d.is_online = device_manager.is_connected(d.id)

    groups_q = await db.execute(
        select(
            DeviceGroup,
            func.count(Device.id).label("device_count"),
        )
        .outerjoin(Device, Device.group_id == DeviceGroup.id)
        .group_by(DeviceGroup.id)
        .order_by(DeviceGroup.name)
    )
    groups = [
        type("GroupRow", (), {"id": g.id, "name": g.name, "description": g.description, "default_asset_id": g.default_asset_id, "device_count": c})()
        for g, c in groups_q.all()
    ]

    assets_q = await db.execute(select(Asset).order_by(Asset.filename))
    assets = assets_q.scalars().all()

    return templates.TemplateResponse(request, "devices.html", {
        "active_tab": "devices",
        "devices": devices,
        "groups": groups,
        "assets": assets,
    })


# ── Assets ──


@router.get("/assets", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def assets_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Asset).order_by(Asset.uploaded_at.desc()))
    assets = result.scalars().all()

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
        select(Device).where(Device.status == DeviceStatus.APPROVED).order_by(Device.name)
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
        label = f"{tz_name} (UTC{sign}{h:02d}:{m:02d})"
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
