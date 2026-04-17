"""Web UI routes — Jinja2 server-rendered pages."""

from fastapi import APIRouter, Depends, Query, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from itsdangerous import URLSafeTimedSerializer
from sqlalchemy import func, select
import sqlalchemy
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from cms.auth import (
    COOKIE_NAME,
    MAX_AGE,
    SETTING_MCP_ENABLED,
    SETTING_MCP_SERVICE_KEY_HASH,
    SETTING_PASSWORD_HASH,
    SETTING_SETUP_COMPLETED,
    SETTING_SMTP_FROM_EMAIL,
    SETTING_SMTP_HOST,
    SETTING_SMTP_PASSWORD,
    SETTING_SMTP_PORT,

    SETTING_SMTP_USERNAME,
    SETTING_TIMEZONE,
    SETTING_USERNAME,
    _resolve_user_from_session,
    get_current_user,
    get_setting,
    get_settings,
    hash_password,
    provision_service_key,
    require_auth,
    require_permission,
    revoke_service_key,
    set_setting,
    verify_password,
)
from cms.config import Settings
from cms.database import get_db
from cms.models.asset import Asset, AssetVariant, VariantStatus
from cms.models.device import Device, DeviceGroup, DeviceStatus
from cms.permissions import USERS_READ, USERS_WRITE, ROLES_WRITE, DEVICES_MANAGE, has_permission
from cms.auth import get_user_group_ids
from cms.models.device_profile import DeviceProfile
from cms.models.schedule import Schedule
from cms.models.schedule_log import ScheduleLog, ScheduleLogEvent
from cms.models.group_asset import GroupAsset
from cms.models.user import User, UserGroup
from cms.services.device_manager import device_manager
from cms.services.audit_service import audit_log
from cms.services.version_checker import get_latest_device_version, is_update_available
from cms.routers.devices import _upgrading as _devices_upgrading

import json as _json
from datetime import datetime, timezone as _tz
from zoneinfo import ZoneInfo

from cms.timezones import build_tz_options, canonical_timezones

from cms.mcp_utils import notify_mcp_reload as _notify_mcp_reload


async def _get_setup_user(request: Request, settings, db):
    """Extract and resolve the user from the session cookie. Returns None if not authenticated."""
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        return None
    return await _resolve_user_from_session(cookie, settings, db)


# Canonical IANA timezones for dropdowns — sorted for readability.
COMMON_TIMEZONES = sorted(canonical_timezones())

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

# Cache-busting: use build timestamp so browsers fetch fresh static files after deploy
import time as _time
_static_version = str(int(_time.time()))
templates.env.globals["static_version"] = _static_version

# Inject CMS version into all templates for the footer
from cms import __version__ as _cms_version
templates.env.globals["cms_version"] = _cms_version

router = APIRouter()


# ── Auth ──


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@router.get("/setup-account")
async def setup_account(
    request: Request,
    token: str = Query(...),
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """One-time magic link from welcome email — logs user in and redirects to password change."""
    result = await db.execute(
        select(User).where(User.setup_token == token, User.is_active.is_(True))
    )
    user = result.scalar_one_or_none()
    if not user:
        return templates.TemplateResponse(
            request, "login.html",
            {"error": "This setup link is invalid or has already been used. Please sign in with your credentials."},
            status_code=400,
        )

    # Invalidate the token (single-use)
    user.setup_token = None
    from datetime import datetime, timezone
    user.last_login_at = datetime.now(timezone.utc)
    await db.commit()

    # Create session and redirect to force-password-change
    serializer = URLSafeTimedSerializer(settings.secret_key)
    session_token = serializer.dumps({"user_id": str(user.id)})
    response = RedirectResponse(url="/force-password-change", status_code=303)
    response.set_cookie(
        COOKIE_NAME, session_token, max_age=MAX_AGE, httponly=True, samesite="lax"
    )
    return response


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


# ── First-run setup wizard ──


@router.get("/setup", response_class=HTMLResponse)
async def setup_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Render the first-run setup wizard."""
    completed = await get_setting(db, SETTING_SETUP_COMPLETED)
    if completed == "true":
        return RedirectResponse(url="/", status_code=303)

    # Require login — redirect to login page if not authenticated
    user = await _get_setup_user(request, settings, db)
    if user is None:
        return RedirectResponse(url="/login", status_code=303)

    tz_options = build_tz_options()
    return templates.TemplateResponse(request, "setup.html", {
        "timezones": tz_options,
        "user": user,
    })


@router.post("/setup/account")
async def setup_account_update(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Step 1: Update the admin account with a new display name, email, and password."""
    completed = await get_setting(db, SETTING_SETUP_COMPLETED)
    if completed == "true":
        return JSONResponse({"error": "Setup already completed"}, status_code=400)

    # Require login
    user = await _get_setup_user(request, settings, db)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    data = await request.json()
    display_name = (data.get("display_name") or "").strip()
    email = (data.get("email") or "").strip()
    password = data.get("password") or ""

    if not display_name:
        return JSONResponse({"error": "Display name is required"}, status_code=400)
    if not email or "@" not in email:
        return JSONResponse({"error": "A valid email address is required"}, status_code=400)
    if len(password) < 6:
        return JSONResponse({"error": "Password must be at least 6 characters"}, status_code=400)

    # Check if email is taken by a different user
    from sqlalchemy import select as sa_select
    existing = await db.execute(
        sa_select(User).where(User.email == email, User.id != user.id)
    )
    if existing.scalar_one_or_none():
        return JSONResponse({"error": "A user with this email already exists"}, status_code=409)

    # Update the current admin account
    user.display_name = display_name
    user.email = email
    user.password_hash = hash_password(password)
    user.must_change_password = False

    await audit_log(
        db, user=user, action="settings.setup.account", resource_type="settings",
        description=f"Setup wizard: updated admin account ({email})",
        details={"display_name": display_name, "email": email},
        request=request,
    )
    await db.commit()

    return JSONResponse({"status": "ok"})


@router.post("/setup/smtp")
async def setup_smtp(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Step 2: Save SMTP configuration (optional — can be skipped)."""
    completed = await get_setting(db, SETTING_SETUP_COMPLETED)
    if completed == "true":
        return JSONResponse({"error": "Setup already completed"}, status_code=400)
    user = await _get_setup_user(request, settings, db)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    data = await request.json()
    await set_setting(db, SETTING_SMTP_HOST, data.get("host", ""))
    await set_setting(db, SETTING_SMTP_PORT, str(data.get("port", 587)))
    await set_setting(db, SETTING_SMTP_USERNAME, data.get("username", ""))
    if data.get("password"):
        await set_setting(db, SETTING_SMTP_PASSWORD, data["password"])
    await set_setting(db, SETTING_SMTP_FROM_EMAIL, data.get("from_email", ""))
    await audit_log(
        db, user=user, action="settings.smtp.update", resource_type="settings",
        description=f"Setup wizard: updated SMTP settings (host={data.get('host', '')})",
        details={
            "host": data.get("host", ""),
            "port": data.get("port"),
            "username": data.get("username", ""),
            "from_email": data.get("from_email", ""),
            "password_changed": bool(data.get("password")),
        },
        request=request,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/setup/smtp/test")
async def setup_smtp_test(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Test SMTP configuration during setup wizard."""
    completed = await get_setting(db, SETTING_SETUP_COMPLETED)
    if completed == "true":
        return JSONResponse({"error": "Setup already completed"}, status_code=400)
    user = await _get_setup_user(request, settings, db)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    data = await request.json()
    to_email = (data.get("to_email") or "").strip()
    if not to_email:
        return JSONResponse({"success": False, "message": "Recipient email required"}, status_code=400)

    from cms.services.email_service import get_smtp_settings, test_smtp_connection
    smtp_cfg = await get_smtp_settings(db)
    success, message = test_smtp_connection(smtp_cfg, to_email)
    return {"success": success, "message": message}


@router.post("/setup/timezone")
async def setup_timezone(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Step 3: Set CMS timezone."""
    completed = await get_setting(db, SETTING_SETUP_COMPLETED)
    if completed == "true":
        return JSONResponse({"error": "Setup already completed"}, status_code=400)
    user = await _get_setup_user(request, settings, db)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    data = await request.json()
    tz = (data.get("timezone") or "").strip()
    if tz not in canonical_timezones():
        return JSONResponse({"error": "Invalid timezone"}, status_code=400)

    await set_setting(db, SETTING_TIMEZONE, tz)
    await audit_log(
        db, user=user, action="settings.timezone.update", resource_type="settings",
        description=f"Setup wizard: set CMS timezone to '{tz}'",
        details={"timezone": tz},
        request=request,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/setup/mcp")
async def setup_mcp(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Step 4: Enable or disable MCP server."""
    completed = await get_setting(db, SETTING_SETUP_COMPLETED)
    if completed == "true":
        return JSONResponse({"error": "Setup already completed"}, status_code=400)
    user = await _get_setup_user(request, settings, db)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    data = await request.json()
    enabled = data.get("enabled", False)
    await set_setting(db, SETTING_MCP_ENABLED, "true" if enabled else "false")
    await audit_log(
        db, user=user, action="settings.mcp.update", resource_type="settings",
        description=f"Setup wizard: {'enabled' if enabled else 'disabled'} MCP server",
        details={"enabled": bool(enabled)},
        request=request,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/setup/complete")
async def setup_complete(
    request: Request,
    settings: Settings = Depends(get_settings),
    db: AsyncSession = Depends(get_db),
):
    """Mark setup as complete — wizard will no longer appear."""
    completed = await get_setting(db, SETTING_SETUP_COMPLETED)
    if completed == "true":
        return JSONResponse({"error": "Setup already completed"}, status_code=400)
    user = await _get_setup_user(request, settings, db)
    if user is None:
        return JSONResponse({"error": "Not authenticated"}, status_code=401)

    await set_setting(db, SETTING_SETUP_COMPLETED, "true")

    # Clear the in-memory cache so middleware stops redirecting
    import cms.main as _main_module
    _main_module._setup_completed_cache = True

    await audit_log(
        db, user=user, action="settings.setup.complete", resource_type="settings",
        description="Setup wizard completed",
        request=request,
    )
    await db.commit()

    return {"status": "ok"}


# ── Dashboard ──


@router.get("/", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    from datetime import datetime as _dt, timezone as _tz
    from cms.services.scheduler import compute_now_playing, get_upcoming_schedules
    from cms.auth import get_user_group_ids

    # CMS timezone
    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)
    now = _dt.now(_tz.utc)

    # Group scoping
    user = getattr(request.state, "user", None)
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None

    # Pending devices (only visible to users with devices:manage)
    user_perms = user.role.permissions if user and user.role else []
    can_manage = has_permission(user_perms, DEVICES_MANAGE)

    if can_manage:
        pending_query = select(Device).where(Device.status == DeviceStatus.PENDING).order_by(Device.registered_at)
        if not is_admin:
            if group_ids:
                pending_query = pending_query.where(
                    (Device.group_id.in_(group_ids)) | (Device.group_id.is_(None))
                )
            else:
                pending_query = pending_query.where(Device.group_id.is_(None))
        pending_q = await db.execute(pending_query)
        pending_devices = pending_q.scalars().all()
        for d in pending_devices:
            d.is_online = device_manager.is_connected(d.id)
    else:
        pending_devices = []

    # All adopted devices
    devices_query = (
        select(Device)
        .options(selectinload(Device.group))
        .where(Device.status == DeviceStatus.ADOPTED)
        .order_by(Device.name, Device.id)
    )
    if not is_admin:
        if group_ids:
            devices_query = devices_query.where(
                (Device.group_id.in_(group_ids)) | (Device.group_id.is_(None))
            )
        else:
            devices_query = devices_query.where(Device.group_id.is_(None))
    devices_q = await db.execute(devices_query)
    all_devices = devices_q.scalars().all()
    for d in all_devices:
        d.is_online = device_manager.is_connected(d.id)

    # Offline devices (adopted but not connected)
    offline_devices = [d for d in all_devices if not d.is_online]

    # Orphaned devices (only visible to users with devices:manage)
    if can_manage:
        orphan_query = (
            select(Device)
            .options(selectinload(Device.group))
            .where(Device.status == DeviceStatus.ORPHANED)
            .order_by(Device.name, Device.id)
        )
        if not is_admin:
            if group_ids:
                orphan_query = orphan_query.where(
                    (Device.group_id.in_(group_ids)) | (Device.group_id.is_(None))
                )
            else:
                orphan_query = orphan_query.where(Device.group_id.is_(None))
        orphaned_q = await db.execute(orphan_query)
        orphaned_devices = orphaned_q.scalars().all()
    else:
        orphaned_devices = []

    # Now Playing — computed from DB + live device state
    now_playing = await compute_now_playing(db, tz, now)
    # Scope now_playing to user's visible devices (admins see all)
    if not is_admin:
        visible_device_ids = {d.id for d in all_devices}
        now_playing = [np for np in now_playing if np["device_id"] in visible_device_ids]

    # Compute remaining_seconds from schedule end times
    from datetime import time as _time, timedelta as _td
    local_now = now.astimezone(tz)
    for np in now_playing:
        raw_end = np.get("end_time_raw")
        raw_start = np.get("start_time_raw")
        if raw_end:
            parts = [int(x) for x in raw_end.split(":")]
            end_t = _time(*parts)
            start_t = None
            if raw_start:
                sp = [int(x) for x in raw_start.split(":")]
                start_t = _time(*sp)
            end_today = _dt.combine(local_now.date(), end_t, tzinfo=tz)
            if start_t and end_t <= start_t:
                end_today += _td(days=1)
            np["remaining_seconds"] = max(0, int((end_today - local_now).total_seconds()))
    live_states = {s["device_id"]: s for s in device_manager.get_all_states()}
    for np in now_playing:
        did = np["device_id"]
        live = live_states.get(did, {})
        actual_mode = live.get("mode", "unknown")
        actual_asset = live.get("asset")
        expected_asset = np.get("asset_raw") or np.get("asset_filename")
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
                if actual_mode != "play":
                    if not device_manager.is_connected(did):
                        np["device_offline"] = True
                    else:
                        np["starting"] = True
        # Outside the grace period, flag offline devices distinctly
        if np.get("mismatch") and not device_manager.is_connected(did):
            np["mismatch"] = False
            np["device_offline"] = True

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
    upcoming_query = (
        select(Schedule)
        .options(
            selectinload(Schedule.asset),
            selectinload(Schedule.group),
        )
        .where(Schedule.enabled == True)
    )
    if not is_admin:
        if group_ids:
            upcoming_query = upcoming_query.where(
                Schedule.group_id.in_(group_ids)
            )
        else:
            upcoming_query = upcoming_query.where(sqlalchemy.false())
    sched_q = await db.execute(upcoming_query)
    all_schedules = sched_q.scalars().all()
    offline_set = set(d.id for d in offline_devices)
    upcoming = get_upcoming_schedules(all_schedules, now, tz, now_playing=now_playing, offline_device_ids=offline_set)
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

    # Groups for the adoption modal dropdown
    # Admins see all groups; scoped users see only their assigned groups.
    adoption_groups_query = select(DeviceGroup).order_by(DeviceGroup.name)
    if not is_admin:
        if group_ids:
            adoption_groups_query = adoption_groups_query.where(DeviceGroup.id.in_(group_ids))
        else:
            adoption_groups_query = adoption_groups_query.where(sqlalchemy.false())
    adoption_groups_q = await db.execute(adoption_groups_query)
    adoption_groups = adoption_groups_q.scalars().all()

    adoption_profiles_q = await db.execute(select(DeviceProfile).order_by(DeviceProfile.name))
    adoption_profiles = adoption_profiles_q.scalars().all()

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
        "adoption_groups": adoption_groups,
        "adoption_profiles": adoption_profiles,
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
async def dashboard_json(request: Request, db: AsyncSession = Depends(get_db)):
    """Lightweight JSON endpoint for dashboard polling."""
    from datetime import datetime as _dt, timezone as _tz
    from cms.services.scheduler import compute_now_playing, get_upcoming_schedules
    from cms.auth import get_user_group_ids

    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)
    now = _dt.now(_tz.utc)

    # Group scoping
    user = getattr(request.state, "user", None)
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None

    def _scope_device_query(q):
        if is_admin:
            return q
        if group_ids:
            return q.where((Device.group_id.in_(group_ids)) | (Device.group_id.is_(None)))
        return q.where(Device.group_id.is_(None))

    # Pending/orphaned device IDs (only for users with devices:manage)
    user_perms = user.role.permissions if user and user.role else []
    can_manage = has_permission(user_perms, DEVICES_MANAGE)

    if can_manage:
        pending_q = await db.execute(
            _scope_device_query(select(Device.id).where(Device.status == DeviceStatus.PENDING))
        )
        pending_ids = [r[0] for r in pending_q.all()]

        orphaned_q = await db.execute(
            _scope_device_query(select(Device.id).where(Device.status == DeviceStatus.ORPHANED))
        )
        orphaned_ids = [r[0] for r in orphaned_q.all()]
    else:
        pending_ids = []
        orphaned_ids = []

    # Online status (adopted devices only)
    devices_q = await db.execute(
        _scope_device_query(select(Device.id).where(Device.status == DeviceStatus.ADOPTED))
    )
    all_device_ids = [r[0] for r in devices_q.all()]
    offline_ids = [did for did in all_device_ids if not device_manager.is_connected(did)]

    now_playing = await compute_now_playing(db, tz, now)
    # Scope now_playing to user's visible devices
    visible_device_set = set(all_device_ids)
    now_playing = [np for np in now_playing if np["device_id"] in visible_device_set]

    # Compute remaining_seconds from schedule end times
    from datetime import time as _time, timedelta as _td
    local_now = now.astimezone(tz)
    for np in now_playing:
        raw_end = np.get("end_time_raw")
        raw_start = np.get("start_time_raw")
        if raw_end:
            parts = [int(x) for x in raw_end.split(":")]
            end_t = _time(*parts)
            start_t = None
            if raw_start:
                sp = [int(x) for x in raw_start.split(":")]
                start_t = _time(*sp)
            end_today = _dt.combine(local_now.date(), end_t, tzinfo=tz)
            if start_t and end_t <= start_t:
                end_today += _td(days=1)
            np["remaining_seconds"] = max(0, int((end_today - local_now).total_seconds()))

    live_states = {s["device_id"]: s for s in device_manager.get_all_states()}
    for np in now_playing:
        did = np["device_id"]
        live = live_states.get(did, {})
        actual_mode = live.get("mode", "unknown")
        actual_asset = live.get("asset")
        np["mismatch"] = actual_mode != "play" or actual_asset != (np.get("asset_raw") or np.get("asset_filename"))
        if np["mismatch"] and np.get("since"):
            since = _dt.fromisoformat(np["since"])
            if (now - since).total_seconds() < 45:
                np["mismatch"] = False
                if actual_mode != "play":
                    if did in offline_ids:
                        np["device_offline"] = True
                    else:
                        np["starting"] = True
        if np.get("mismatch") and did in offline_ids:
            np["mismatch"] = False
            np["device_offline"] = True

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
    upcoming_q = (
        select(Schedule)
        .options(
            selectinload(Schedule.asset),
            selectinload(Schedule.group),
        )
        .where(Schedule.enabled == True)
    )
    if not is_admin:
        if group_ids:
            upcoming_q = upcoming_q.where(
                Schedule.group_id.in_(group_ids)
            )
        else:
            upcoming_q = upcoming_q.where(sqlalchemy.false())
    sched_q = await db.execute(upcoming_q)
    all_schedules = sched_q.scalars().all()
    offline_set = set(offline_ids)
    upcoming = get_upcoming_schedules(all_schedules, now, tz, now_playing=now_playing, offline_device_ids=offline_set)

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
    from cms.services.scheduler import compute_now_playing
    from cms.auth import get_user_group_ids, SETTING_TIMEZONE, get_setting as _get_setting
    from zoneinfo import ZoneInfo

    tz_name = await _get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)
    now = datetime.now(_tz.utc)

    user = getattr(request.state, "user", None)
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None

    # Only show pending/orphaned devices to users with devices:manage
    user_perms = user.role.permissions if user and user.role else []
    can_manage = has_permission(user_perms, DEVICES_MANAGE)

    device_query = select(Device).options(selectinload(Device.group)).order_by(Device.name, Device.id)
    if not can_manage:
        device_query = device_query.where(Device.status == DeviceStatus.ADOPTED)
    if not is_admin:
        if group_ids:
            device_query = device_query.where(
                (Device.group_id.in_(group_ids)) | (Device.group_id.is_(None))
            )
        else:
            device_query = device_query.where(Device.group_id.is_(None))

    result = await db.execute(device_query)
    devices = result.scalars().all()
    live_states = {s["device_id"]: s for s in device_manager.get_all_states()}
    scheduled_device_ids = {np["device_id"] for np in await compute_now_playing(db, tz, now)}

    # Build URL→display name map for resolving playback_asset on URL-based assets
    assets_early_q = await db.execute(
        select(Asset).where(Asset.deleted_at.is_(None)).order_by(Asset.filename)
    )
    assets_early = assets_early_q.scalars().all()
    _url_display = {}
    for a in assets_early:
        if a.url:
            _url_display.setdefault(a.url, a.filename)
    for d in devices:
        d.is_online = device_manager.is_connected(d.id)
        state = live_states.get(d.id)
        d.cpu_temp_c = state["cpu_temp_c"] if state else None
        d.ip_address = state["ip_address"] if state else None
        d.playback_mode = state["mode"] if state else None
        d.playback_asset = state["asset"] if state else None
        if d.playback_asset and d.playback_asset in _url_display:
            d.playback_asset = _url_display[d.playback_asset]
        d.pipeline_state = state["pipeline_state"] if state else None
        d.started_at = state["started_at"] if state else None
        d.playback_position_ms = state["playback_position_ms"] if state else None
        d.ssh_enabled = state["ssh_enabled"] if state else None
        d.local_api_enabled = state["local_api_enabled"] if state else None
        d.update_available = is_update_available(d.firmware_version)
        d.is_upgrading = d.id in _devices_upgrading
        d.has_active_schedule = d.id in scheduled_device_ids

    groups_query = (
        select(DeviceGroup)
        .options(selectinload(DeviceGroup.devices))
        .order_by(DeviceGroup.name)
    )
    if not is_admin:
        if group_ids:
            groups_query = groups_query.where(DeviceGroup.id.in_(group_ids))
        else:
            groups_query = groups_query.where(sqlalchemy.false())

    groups_q = await db.execute(groups_query)
    groups = groups_q.scalars().all()

    # Count schedules per group (for disabling delete button)
    from cms.models.schedule import Schedule as ScheduleModel
    group_sched_counts: dict[uuid.UUID, int] = {}
    if groups:
        sched_rows = (await db.execute(
            select(ScheduleModel.group_id, func.count())
            .where(ScheduleModel.group_id.in_([g.id for g in groups]))
            .group_by(ScheduleModel.group_id)
        )).all()
        group_sched_counts = {gid: cnt for gid, cnt in sched_rows}

    # Attach device_count, schedule_count, and is_online to each group's devices
    for g in groups:
        g.device_count = len(g.devices)
        g.schedule_count = group_sched_counts.get(g.id, 0)
        for d in g.devices:
            d.is_online = device_manager.is_connected(d.id)
            state = live_states.get(d.id)
            d.cpu_temp_c = state["cpu_temp_c"] if state else None
            d.ip_address = state["ip_address"] if state else None
            d.playback_mode = state["mode"] if state else None
            d.playback_asset = state["asset"] if state else None
            if d.playback_asset and d.playback_asset in _url_display:
                d.playback_asset = _url_display[d.playback_asset]
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

    assets = assets_early

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
    all_ug = ug_q.scalars().all()
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

    # Group permissions by prefix (e.g. "devices", "schedules") for collapsible UI
    from collections import OrderedDict
    grouped_permissions: dict[str, list[str]] = OrderedDict()
    for perm in all_permissions:
        prefix = perm.split(":")[0] if ":" in perm else perm
        grouped_permissions.setdefault(prefix, []).append(perm)

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
        "grouped_permissions": grouped_permissions,
        "perm_descriptions": perm_descriptions,
        "can_write": can_write,
        "can_write_roles": can_write_roles,
        "current_user_id": str(user.id),
    })


# ── Profile ──


@router.get("/profile", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def profile_page(request: Request, db: AsyncSession = Depends(get_db)):
    user: User = request.state.user
    return templates.TemplateResponse(request, "profile.html", {
        "active_tab": "profile",
        "profile_user": user,
        "pw_success": None,
        "pw_error": None,
    })


@router.post("/profile/password", response_class=HTMLResponse)
async def profile_change_password(
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    form = await request.form()
    current_password = form.get("current_password", "")
    new_password = form.get("new_password", "")
    confirm_password = form.get("confirm_password", "")

    ctx = {
        "active_tab": "profile",
        "profile_user": current_user,
        "pw_success": None,
        "pw_error": None,
    }

    if not verify_password(current_password, current_user.password_hash):
        ctx["pw_error"] = "Current password is incorrect"
        return templates.TemplateResponse(request, "profile.html", ctx, status_code=400)

    if len(new_password) < 6:
        ctx["pw_error"] = "New password must be at least 6 characters"
        return templates.TemplateResponse(request, "profile.html", ctx, status_code=400)

    if new_password != confirm_password:
        ctx["pw_error"] = "New passwords do not match"
        return templates.TemplateResponse(request, "profile.html", ctx, status_code=400)

    current_user.password_hash = hash_password(new_password)
    await db.commit()
    await set_setting(db, SETTING_PASSWORD_HASH, current_user.password_hash)

    ctx["pw_success"] = "Password updated successfully"
    return templates.TemplateResponse(request, "profile.html", ctx)


# ── Assets ──


@router.get("/assets", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def assets_page(request: Request, db: AsyncSession = Depends(get_db)):
    user: User | None = getattr(request.state, "user", None)

    # ── Determine which assets this user can see ──
    group_ids = await get_user_group_ids(user, db) if user else []
    # group_ids is None for admins (see all), list of UUIDs for others
    asset_q = (
        select(Asset)
        .where(Asset.deleted_at.is_(None))
        .options(selectinload(Asset.variants).selectinload(AssetVariant.profile))
        .order_by(Asset.uploaded_at.desc())
    )
    if group_ids is not None:
        # Non-admin: only global + assets in their groups + own unshared uploads
        global_ids = set(
            (await db.execute(select(Asset.id).where(Asset.is_global.is_(True)))).scalars().all()
        )
        ga_ids = set()
        if group_ids:
            ga_ids = set(
                (await db.execute(
                    select(GroupAsset.asset_id).where(GroupAsset.group_id.in_(group_ids))
                )).scalars().all()
            )
        own_ids = set()
        if user:
            own_ids = set(
                (await db.execute(
                    select(Asset.id)
                    .where(Asset.uploaded_by_user_id == user.id)
                    .where(~Asset.id.in_(select(GroupAsset.asset_id)))
                )).scalars().all()
            )
        visible = list(global_ids | ga_ids | own_ids)
        asset_q = asset_q.where(Asset.id.in_(visible))

    result = await db.execute(asset_q)
    assets = result.scalars().all()

    # Count schedules per asset
    sched_counts_q = await db.execute(
        select(Schedule.asset_id, func.count()).group_by(Schedule.asset_id)
    )
    sched_counts = dict(sched_counts_q.all())

    # Annotate each asset with variant summary + schedule count + group info
    all_group_assets = {}
    if assets:
        ga_rows = (await db.execute(
            select(GroupAsset).where(GroupAsset.asset_id.in_([a.id for a in assets]))
        )).scalars().all()
        for ga in ga_rows:
            all_group_assets.setdefault(ga.asset_id, []).append(ga)

    for a in assets:
        # Sort variants by profile name for consistent display order
        a.variants.sort(key=lambda v: (v.profile.name if v.profile else ""))
        total = len(a.variants)
        ready = sum(1 for v in a.variants if v.status == VariantStatus.READY)
        processing = sum(1 for v in a.variants if v.status == VariantStatus.PROCESSING)
        failed = sum(1 for v in a.variants if v.status == VariantStatus.FAILED)
        a.variant_total = total
        a.variant_ready = ready
        a.variant_processing = processing
        a.variant_failed = failed
        a.schedule_count = sched_counts.get(a.id, 0)
        entries = all_group_assets.get(a.id, [])
        # Non-admin users should only see group entries for their own groups
        if group_ids is not None:
            entries = [ga for ga in entries if ga.group_id in group_ids]
        a.group_asset_entries = entries

    # Groups available for upload dropdown (user's groups, or all for admin)
    if group_ids is None:
        groups_q = await db.execute(select(DeviceGroup).order_by(DeviceGroup.name))
    elif group_ids:
        groups_q = await db.execute(
            select(DeviceGroup).where(DeviceGroup.id.in_(group_ids)).order_by(DeviceGroup.name)
        )
    else:
        groups_q = None
    user_groups = groups_q.scalars().all() if groups_q else []

    # Build lookup of group names — scoped to user's groups for non-admins
    if group_ids is None:
        all_groups_q = await db.execute(select(DeviceGroup))
    elif group_ids:
        all_groups_q = await db.execute(
            select(DeviceGroup).where(DeviceGroup.id.in_(group_ids))
        )
    else:
        all_groups_q = None
    group_name_map = {str(g.id): g.name for g in all_groups_q.scalars().all()} if all_groups_q else {}

    is_admin = group_ids is None

    # Build uploader name map for admin view
    uploader_map: dict[str, str] = {}
    if is_admin and assets:
        uploader_ids = {a.uploaded_by_user_id for a in assets if a.uploaded_by_user_id}
        if uploader_ids:
            uploaders = (await db.execute(
                select(User.id, User.username, User.email).where(User.id.in_(uploader_ids))
            )).all()
            uploader_map = {str(u.id): u.username or u.email for u in uploaders}

    return templates.TemplateResponse(request, "assets.html", {
        "active_tab": "assets",
        "assets": assets,
        "user_groups": user_groups,
        "group_name_map": group_name_map,
        "is_admin": is_admin,
        "uploader_map": uploader_map,
    })





# ── Schedules ──


@router.get("/schedules", response_class=HTMLResponse, dependencies=[Depends(require_auth)])
async def schedules_page(request: Request, db: AsyncSession = Depends(get_db)):
    user: User | None = getattr(request.state, "user", None)

    # Filter assets by user visibility
    group_ids = await get_user_group_ids(user, db) if user else []
    is_admin = group_ids is None

    sched_query = (
        select(Schedule)
        .options(
            selectinload(Schedule.asset),
            selectinload(Schedule.group),
        )
        .order_by(Schedule.priority.desc(), Schedule.name)
    )
    if not is_admin:
        if group_ids:
            sched_query = sched_query.where(
                Schedule.group_id.in_(group_ids)
            )
        else:
            sched_query = sched_query.where(sqlalchemy.false())

    schedules_q = await db.execute(sched_query)
    all_schedules = schedules_q.scalars().all()
    asset_q = select(Asset).where(Asset.deleted_at.is_(None)).order_by(Asset.filename)
    if not is_admin:
        from cms.models.asset import Asset as AssetModel
        global_ids = set(
            (await db.execute(select(Asset.id).where(Asset.is_global.is_(True)))).scalars().all()
        )
        ga_ids = set()
        if group_ids:
            ga_ids = set(
                (await db.execute(
                    select(GroupAsset.asset_id).where(GroupAsset.group_id.in_(group_ids))
                )).scalars().all()
            )
        own_ids = set()
        if user:
            own_ids = set(
                (await db.execute(
                    select(Asset.id)
                    .where(Asset.uploaded_by_user_id == user.id)
                    .where(~Asset.id.in_(select(GroupAsset.asset_id)))
                )).scalars().all()
            )
        visible = list(global_ids | ga_ids | own_ids)
        asset_q = asset_q.where(Asset.id.in_(visible))
    assets_result = await db.execute(asset_q)
    assets = assets_result.scalars().all()

    # Load asset-to-group mapping for JS filtering
    asset_group_map = {}
    if assets:
        ga_rows = (await db.execute(
            select(GroupAsset.asset_id, GroupAsset.group_id)
            .where(GroupAsset.asset_id.in_([a.id for a in assets]))
        )).all()
        for aid, gid in ga_rows:
            asset_group_map.setdefault(str(aid), []).append(str(gid))
    for a in assets:
        a._group_ids_json = asset_group_map.get(str(a.id), [])

    groups_query_sched = select(DeviceGroup).order_by(DeviceGroup.name)
    if not is_admin:
        if group_ids:
            groups_query_sched = groups_query_sched.where(DeviceGroup.id.in_(group_ids))
        else:
            groups_query_sched = groups_query_sched.where(sqlalchemy.false())
    groups_q = await db.execute(groups_query_sched)
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

    tz_options = build_tz_options()

    # Determine which schedules are currently playing on at least one device
    from cms.services.scheduler import get_now_playing as _get_now_playing
    playing_schedule_ids = list({
        np["schedule_id"] for np in _get_now_playing() if "schedule_id" in np
    })

    return templates.TemplateResponse(request, "schedules.html", {
        "active_tab": "schedules",
        "schedules": active_schedules,
        "expired_schedules": expired_schedules,
        "assets": assets,
        "groups": groups,
        "current_timezone": current_timezone,
        "timezone_saved": timezone_saved,
        "tz_options": tz_options,
        "is_admin": is_admin,
        "playing_schedule_ids": playing_schedule_ids,
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


@router.get("/settings", response_class=HTMLResponse,
            dependencies=[Depends(require_permission("settings:write"))])
async def settings_page(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    mcp_enabled = (await get_setting(db, SETTING_MCP_ENABLED)) == "true"

    # SMTP settings
    smtp_host = await get_setting(db, SETTING_SMTP_HOST) or ""
    smtp_port = await get_setting(db, SETTING_SMTP_PORT) or "587"
    smtp_username = await get_setting(db, SETTING_SMTP_USERNAME) or ""
    smtp_password = await get_setting(db, SETTING_SMTP_PASSWORD) or ""
    smtp_from_email = await get_setting(db, SETTING_SMTP_FROM_EMAIL) or ""

    # Timezone
    current_timezone = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    timezone_saved = (await get_setting(db, SETTING_TIMEZONE)) is not None
    tz_options = build_tz_options()

    # Device list for log download panel
    from cms.models.device import Device
    result = await db.execute(select(Device).order_by(Device.name))
    devices = result.scalars().all()
    device_list = [
        {"id": d.id, "name": d.name, "connected": device_manager.is_connected(d.id)}
        for d in devices
    ]

    # Alert settings
    alert_offline_grace = await get_setting(db, "alert_offline_grace_seconds") or "120"
    alert_temp_warning = await get_setting(db, "alert_temp_warning_c") or "70"
    alert_temp_critical = await get_setting(db, "alert_temp_critical_c") or "80"
    alert_temp_cooldown = await get_setting(db, "alert_temp_cooldown_seconds") or "300"
    alert_email_enabled = (await get_setting(db, "email_notifications_enabled")) == "true"

    return templates.TemplateResponse(request, "settings.html", {
        "active_tab": "settings",
        "mcp_enabled": mcp_enabled,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_username": smtp_username,
        "smtp_password": smtp_password,
        "smtp_from_email": smtp_from_email,
        "current_timezone": current_timezone,
        "timezone_saved": timezone_saved,
        "timezones": tz_options,
        "devices": device_list,
        "alert_offline_grace": alert_offline_grace,
        "alert_temp_warning": alert_temp_warning,
        "alert_temp_critical": alert_temp_critical,
        "alert_temp_cooldown": alert_temp_cooldown,
        "alert_email_enabled": alert_email_enabled,
        "success": None,
        "error": None,
    })


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


@router.get("/api/system/health", dependencies=[Depends(require_auth)])
async def system_health(db: AsyncSession = Depends(get_db)):
    """Aggregated health check for header status lights."""
    from sqlalchemy import text
    import httpx

    # DB health
    db_status = {"online": False, "detail": "Connection failed"}
    try:
        await db.execute(text("SELECT 1"))
        db_status["online"] = True
        db_status["detail"] = "Connected"
    except Exception as e:
        db_status["detail"] = f"Connection failed: {type(e).__name__}"

    # SMTP configured (not a live connectivity test)
    smtp_host = await get_setting(db, SETTING_SMTP_HOST) or ""
    if smtp_host.strip():
        smtp_status = {"configured": True, "detail": f"Host: {smtp_host.strip()}"}
    else:
        smtp_status = {"configured": False, "detail": "Not configured"}

    # MCP health
    mcp_enabled = (await get_setting(db, SETTING_MCP_ENABLED)) == "true"
    mcp_status = {"online": False, "enabled": mcp_enabled}
    if not mcp_enabled:
        mcp_status["detail"] = "Disabled"
    elif mcp_enabled:
        try:
            settings = get_settings()
            mcp_url = settings.mcp_server_url.rstrip("/")
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{mcp_url}/health")
                mcp_status["online"] = resp.status_code == 200
                mcp_status["detail"] = "Connected" if resp.status_code == 200 else f"Unhealthy (HTTP {resp.status_code})"
        except httpx.ConnectError:
            mcp_status["detail"] = "Connection refused"
        except httpx.TimeoutException:
            mcp_status["detail"] = "Connection timed out"
        except Exception as e:
            mcp_status["detail"] = f"Error: {type(e).__name__}"

    return {"db": db_status, "smtp": smtp_status, "mcp": mcp_status}


@router.post("/api/mcp/toggle", dependencies=[Depends(require_auth)])
async def mcp_toggle(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    """Enable or disable the MCP server.

    When enabling, auto-generates an MCP service key and writes it to the
    shared volume for the MCP container to pick up.
    When disabling, revokes the service key and clears the file.
    """
    body = await request.json()
    enabled = body.get("enabled", False)
    await set_setting(db, SETTING_MCP_ENABLED, "true" if enabled else "false")

    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="settings.mcp.toggle", resource_type="settings",
        description=f"{'Enabled' if enabled else 'Disabled'} MCP server",
        details={"enabled": bool(enabled)},
        request=request,
    )

    if enabled:
        # Auto-provision service key if not already present
        existing = await get_setting(db, SETTING_MCP_SERVICE_KEY_HASH)
        if not existing:
            raw_key, prefix = await provision_service_key(
                db, settings.service_key_path, keyvault_uri=settings.azure_keyvault_uri
            )
            await _notify_mcp_reload(settings)
            await db.commit()
            return {"enabled": enabled, "service_key": raw_key}
    else:
        # Revoke service key when MCP is disabled
        await revoke_service_key(
            db, settings.service_key_path, keyvault_uri=settings.azure_keyvault_uri
        )
        await _notify_mcp_reload(settings)

    await db.commit()
    return {"enabled": enabled}


# ── SMTP Settings ──


@router.post("/api/settings/smtp")
async def save_smtp_settings(
    request: Request,
    _user: User = Depends(require_permission("settings:write")),
    db: AsyncSession = Depends(get_db),
):
    """Save SMTP configuration to the database."""
    data = await request.json()
    await set_setting(db, SETTING_SMTP_HOST, data.get("host", ""))
    await set_setting(db, SETTING_SMTP_PORT, str(data.get("port", 587)))
    await set_setting(db, SETTING_SMTP_USERNAME, data.get("username", ""))
    if "password" in data and data["password"]:
        await set_setting(db, SETTING_SMTP_PASSWORD, data["password"])
    await set_setting(db, SETTING_SMTP_FROM_EMAIL, data.get("from_email", ""))
    await audit_log(
        db, user=_user, action="settings.smtp.update", resource_type="settings",
        description=f"Updated SMTP settings (host={data.get('host', '')})",
        details={
            "host": data.get("host", ""),
            "port": data.get("port"),
            "username": data.get("username", ""),
            "from_email": data.get("from_email", ""),
            "password_changed": bool(data.get("password")),
        },
        request=request,
    )
    await db.commit()
    return {"status": "ok"}


@router.post("/api/settings/alerts")
async def save_alert_settings(
    request: Request,
    db: AsyncSession = Depends(get_db),
    _user: User = Depends(require_permission("settings:write")),
):
    body = await request.json()

    await set_setting(db, "alert_offline_grace_seconds", str(int(body.get("offline_grace_seconds", 120))))
    await set_setting(db, "alert_temp_warning_c", str(float(body.get("temp_warning_c", 70))))
    await set_setting(db, "alert_temp_critical_c", str(float(body.get("temp_critical_c", 80))))
    await set_setting(db, "alert_temp_cooldown_seconds", str(int(body.get("temp_cooldown_seconds", 300))))
    await set_setting(db, "email_notifications_enabled", "true" if body.get("email_notifications_enabled") else "false")

    await audit_log(
        db, user=_user, action="settings.alerts.update", resource_type="settings",
        description="Updated alert settings",
        details={
            "offline_grace_seconds": body.get("offline_grace_seconds"),
            "temp_warning_c": body.get("temp_warning_c"),
            "temp_critical_c": body.get("temp_critical_c"),
            "temp_cooldown_seconds": body.get("temp_cooldown_seconds"),
            "email_notifications_enabled": bool(body.get("email_notifications_enabled")),
        },
        request=request,
    )
    await db.commit()
    return {"ok": True}


@router.post("/api/settings/smtp/test")
async def test_smtp(
    request: Request,
    _user: User = Depends(require_permission("settings:write")),
    db: AsyncSession = Depends(get_db),
):
    """Send a test email to verify SMTP configuration."""
    data = await request.json()
    to_email = data.get("to_email", "")
    if not to_email:
        return JSONResponse({"success": False, "message": "Recipient email required"}, status_code=400)

    from cms.services.email_service import get_smtp_settings, test_smtp_connection
    smtp_cfg = await get_smtp_settings(db)
    success, message = test_smtp_connection(smtp_cfg, to_email)
    return {"success": success, "message": message}


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


@router.post("/settings/timezone", response_class=HTMLResponse,
             dependencies=[Depends(require_permission("settings:write"))])
async def change_timezone(
    request: Request,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    form = await request.form()
    tz_name = form.get("timezone", "").strip()

    mcp_enabled = (await get_setting(db, SETTING_MCP_ENABLED)) == "true"

    tz_options = build_tz_options()

    from cms.models.device import Device
    result = await db.execute(select(Device).order_by(Device.name))
    devices = result.scalars().all()
    device_list = [
        {"id": d.id, "name": d.name, "connected": device_manager.is_connected(d.id)}
        for d in devices
    ]

    # SMTP settings for re-rendering
    smtp_host = await get_setting(db, SETTING_SMTP_HOST) or ""
    smtp_port = await get_setting(db, SETTING_SMTP_PORT) or "587"
    smtp_username = await get_setting(db, SETTING_SMTP_USERNAME) or ""
    smtp_password = await get_setting(db, SETTING_SMTP_PASSWORD) or ""
    smtp_from_email = await get_setting(db, SETTING_SMTP_FROM_EMAIL) or ""

    base_ctx = {
        "active_tab": "settings",
        "mcp_enabled": mcp_enabled,
        "smtp_host": smtp_host,
        "smtp_port": smtp_port,
        "smtp_username": smtp_username,
        "smtp_password": smtp_password,
        "smtp_from_email": smtp_from_email,
        "timezones": tz_options,
        "devices": device_list,
    }

    if tz_name not in canonical_timezones():
        current_timezone = await get_setting(db, SETTING_TIMEZONE) or "UTC"
        return templates.TemplateResponse(request, "settings.html", {
            **base_ctx,
            "current_timezone": current_timezone,
            "timezone_saved": True,
            "success": None,
            "error": f"Invalid timezone: {tz_name}",
        }, status_code=400)

    await set_setting(db, SETTING_TIMEZONE, tz_name)
    await audit_log(
        db, user=getattr(request.state, "user", None),
        action="settings.timezone.update", resource_type="settings",
        description=f"Set CMS timezone to '{tz_name}'",
        details={"timezone": tz_name},
        request=request,
    )
    await db.commit()

    return templates.TemplateResponse(request, "settings.html", {
        **base_ctx,
        "current_timezone": tz_name,
        "timezone_saved": True,
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


# ── Event Log ──


@router.get("/event-log", response_class=HTMLResponse)
async def event_log_page(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
    event_type: str = Query(""),
    device_id: str = Query(""),
    group_id: str = Query(""),
    since: str = Query(""),
    until: str = Query(""),
    user: User = Depends(require_permission("devices:read")),
    db: AsyncSession = Depends(get_db),
):
    from cms.models.device_event import DeviceEvent
    from cms.permissions import GROUPS_VIEW_ALL

    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)

    user_perms = user.role.permissions if user.role else []

    # RBAC: restrict to user's groups unless they have view_all.
    # System events (device_id IS NULL, e.g. CMS started/stopped) are always visible.
    rbac_conditions = []
    if GROUPS_VIEW_ALL not in user_perms:
        from sqlalchemy import or_
        gid_result = await db.execute(
            select(UserGroup.group_id).where(UserGroup.user_id == user.id)
        )
        user_gids = [r[0] for r in gid_result.all()]
        if user_gids:
            rbac_conditions.append(
                or_(
                    DeviceEvent.device_id.is_(None),
                    DeviceEvent.group_id.in_(user_gids),
                )
            )
        else:
            rbac_conditions.append(DeviceEvent.device_id.is_(None))

    # Build filter conditions
    conditions = list(rbac_conditions)
    if event_type.strip():
        conditions.append(DeviceEvent.event_type == event_type.strip())
    if device_id.strip():
        conditions.append(DeviceEvent.device_id == device_id.strip())
    if group_id.strip():
        import uuid as _uuid
        try:
            gid_val = _uuid.UUID(group_id.strip())
            conditions.append(DeviceEvent.group_id == gid_val)
        except ValueError:
            pass
    if since.strip():
        from datetime import datetime as _dt
        try:
            since_dt = _dt.fromisoformat(since.strip())
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=tz)
            conditions.append(DeviceEvent.created_at >= since_dt)
        except ValueError:
            pass
    if until.strip():
        from datetime import datetime as _dt
        try:
            until_dt = _dt.fromisoformat(until.strip())
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=tz)
            conditions.append(DeviceEvent.created_at <= until_dt)
        except ValueError:
            pass

    # Count
    count_q = select(func.count()).select_from(DeviceEvent)
    for cond in conditions:
        count_q = count_q.where(cond)
    count_result = await db.execute(count_q)
    total = count_result.scalar()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    # Main query
    offset = (page - 1) * per_page
    query = (
        select(DeviceEvent)
        .order_by(DeviceEvent.created_at.desc())
        .limit(per_page)
        .offset(offset)
    )
    for cond in conditions:
        query = query.where(cond)
    result = await db.execute(query)
    events = result.scalars().all()

    # Distinct devices for filter dropdown (RBAC-filtered, excludes system events)
    dev_q = (
        select(DeviceEvent.device_id, DeviceEvent.device_name)
        .distinct()
        .where(DeviceEvent.device_id.isnot(None))
    )
    for cond in rbac_conditions:
        dev_q = dev_q.where(cond)
    dev_result = await db.execute(dev_q.order_by(DeviceEvent.device_name))
    available_devices = [(r[0], r[1]) for r in dev_result.all()]

    # Distinct groups for filter dropdown (RBAC-filtered)
    grp_q = (
        select(DeviceEvent.group_id, DeviceEvent.group_name)
        .distinct()
        .where(DeviceEvent.group_id.isnot(None))
    )
    for cond in rbac_conditions:
        grp_q = grp_q.where(cond)
    grp_result = await db.execute(grp_q.order_by(DeviceEvent.group_name))
    available_groups = [(str(r[0]), r[1]) for r in grp_result.all()]

    return templates.TemplateResponse(request, "event_log.html", {
        "active_tab": "event_log",
        "tz": tz,
        "events": events,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total": total,
        "filter_event_type": event_type.strip(),
        "filter_device_id": device_id.strip(),
        "filter_group_id": group_id.strip(),
        "filter_since": since.strip(),
        "filter_until": until.strip(),
        "available_devices": available_devices,
        "available_groups": available_groups,
    })


# ── Audit Log ──


@router.get("/audit", response_class=HTMLResponse)
async def audit_page(
    request: Request,
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=10, le=100),
    q: str = Query(""),
    action: str = Query(""),
    user_id: str = Query(""),
    since: str = Query(""),
    until: str = Query(""),
    _user: User = Depends(require_permission("audit:read")),
    db: AsyncSession = Depends(get_db),
):
    from cms.models.audit_log import AuditLog

    tz_name = await get_setting(db, SETTING_TIMEZONE) or "UTC"
    tz = ZoneInfo(tz_name)

    # Build filter conditions
    conditions = []
    if q.strip():
        like_q = f"%{q.strip()}%"
        from sqlalchemy import or_
        conditions.append(
            or_(
                AuditLog.description.ilike(like_q),
                AuditLog.action.ilike(like_q),
                AuditLog.resource_type.ilike(like_q),
            )
        )
    if action.strip():
        conditions.append(AuditLog.action == action.strip())
    if user_id.strip():
        uval = user_id.strip()
        # Try as UUID (real user FK), else match actor_username in details
        import uuid as _uuid
        try:
            uid = _uuid.UUID(uval)
            conditions.append(AuditLog.user_id == uid)
        except ValueError:
            conditions.append(
                AuditLog.details["actor_username"].astext == uval
            )
    if since.strip():
        from datetime import datetime as _dt
        try:
            since_dt = _dt.fromisoformat(since.strip())
            if since_dt.tzinfo is None:
                since_dt = since_dt.replace(tzinfo=tz)
            conditions.append(AuditLog.created_at >= since_dt)
        except ValueError:
            pass
    if until.strip():
        from datetime import datetime as _dt
        try:
            until_dt = _dt.fromisoformat(until.strip())
            if until_dt.tzinfo is None:
                until_dt = until_dt.replace(tzinfo=tz)
            conditions.append(AuditLog.created_at <= until_dt)
        except ValueError:
            pass

    # Count with filters
    count_q = select(func.count()).select_from(AuditLog)
    for cond in conditions:
        count_q = count_q.where(cond)
    count_result = await db.execute(count_q)
    total = count_result.scalar()
    total_pages = max(1, (total + per_page - 1) // per_page)
    page = min(page, total_pages)

    offset = (page - 1) * per_page
    query = (
        select(AuditLog)
        .options(selectinload(AuditLog.user))
        .order_by(AuditLog.created_at.desc())
        .limit(per_page)
        .offset(offset)
    )
    for cond in conditions:
        query = query.where(cond)
    result = await db.execute(query)
    entries = result.scalars().all()

    # Distinct actions and users for filter dropdowns
    actions_result = await db.execute(
        select(AuditLog.action).distinct().order_by(AuditLog.action)
    )
    available_actions = [r[0] for r in actions_result.all()]

    # Build user dropdown: combine real User records with actor_username from details
    from cms.models.user import User as UserModel
    from sqlalchemy import cast, String

    # Real users linked by FK
    user_ids_result = await db.execute(
        select(AuditLog.user_id).distinct().where(AuditLog.user_id.isnot(None))
    )
    audit_user_ids = [r[0] for r in user_ids_result.all()]
    user_map: dict[str, str] = {}  # value -> display label
    if audit_user_ids:
        users_result = await db.execute(
            select(UserModel).where(UserModel.id.in_(audit_user_ids))
        )
        for u in users_result.scalars().all():
            user_map[str(u.id)] = u.display_name or u.username

    # Also pull distinct actor_username from details JSON
    actor_result = await db.execute(
        select(AuditLog.details["actor_username"].astext)
        .distinct()
        .where(AuditLog.details["actor_username"].astext.isnot(None))
        .where(AuditLog.details["actor_username"].astext != "")
    )
    for r in actor_result.all():
        name = r[0]
        if name and name not in user_map.values():
            user_map[name] = name

    audit_users = sorted(user_map.items(), key=lambda x: x[1].lower())

    return templates.TemplateResponse(request, "audit.html", {
        "active_tab": "audit",
        "tz": tz,
        "entries": entries,
        "page": page,
        "per_page": per_page,
        "total_pages": total_pages,
        "total": total,
        "filter_q": q.strip(),
        "filter_action": action.strip(),
        "filter_user_id": user_id.strip(),
        "filter_since": since.strip(),
        "filter_until": until.strip(),
        "available_actions": available_actions,
        "audit_users": audit_users,
    })
