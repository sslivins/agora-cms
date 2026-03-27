"""Web UI routes — Jinja2 server-rendered pages."""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import COOKIE_NAME, MAX_AGE, get_settings, require_auth
from cms.config import Settings
from cms.database import get_db
from cms.models.asset import Asset
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.models.registration_token import RegistrationToken
from cms.models.schedule import Schedule
from cms.services.device_manager import device_manager

templates = Jinja2Templates(directory="cms/templates")

# Custom Jinja2 filter for days of week
def select_days(day_names, day_numbers):
    return ", ".join(day_names[i - 1] for i in sorted(day_numbers) if 1 <= i <= 7)

templates.env.filters["select_days"] = select_days

router = APIRouter()


# ── Auth ──


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.post("/login")
async def login_submit(request: Request, settings: Settings = Depends(get_settings)):
    form = await request.form()
    username = form.get("username", "")
    password = form.get("password", "")

    if username == settings.admin_username and password == settings.admin_password:
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

    return templates.TemplateResponse(request, "dashboard.html", {
        "active_tab": "dashboard",
        "device_count": device_count,
        "online_count": online_count,
        "pending_count": pending_count,
        "asset_count": asset_count,
        "schedule_count": schedule_count,
        "pending_devices": pending_devices,
        "recent_devices": recent_devices,
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
        type("GroupRow", (), {"id": g.id, "name": g.name, "description": g.description, "device_count": c})()
        for g, c in groups_q.all()
    ]

    return templates.TemplateResponse(request, "devices.html", {
        "active_tab": "devices",
        "devices": devices,
        "groups": groups,
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

    return templates.TemplateResponse(request, "schedules.html", {
        "active_tab": "schedules",
        "schedules": schedules,
        "assets": assets,
        "devices": devices,
        "groups": groups,
    })


# ── Tokens ──


@router.get("/tokens", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def tokens_page(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(RegistrationToken).order_by(RegistrationToken.created_at.desc())
    )
    tokens = result.scalars().all()

    return templates.TemplateResponse(request, "tokens.html", {
        "active_tab": "tokens",
        "tokens": tokens,
    })
