"""Agora CMS — FastAPI application entry point."""

import asyncio
import logging
from collections import deque
from contextlib import asynccontextmanager

# ── In-memory log buffer for log download feature ──
_log_buffer: deque[str] = deque(maxlen=50_000)


class _BufferHandler(logging.Handler):
    """Appends formatted log lines to an in-memory ring buffer."""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            _log_buffer.append(self.format(record))
        except Exception:
            pass


_fmt = "%(asctime)s %(name)s %(levelname)s %(message)s"
logging.basicConfig(level=logging.INFO, format=_fmt)
_buf_handler = _BufferHandler(logging.INFO)
_buf_handler.setFormatter(logging.Formatter(_fmt))
logging.getLogger().addHandler(_buf_handler)

from fastapi import Depends, FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from cms import __version__
from cms.auth import ensure_admin_credentials, get_settings
from cms.database import create_tables, dispose_db, get_db, init_db, run_migrations, wait_for_db
from cms.models import *  # noqa: F401,F403 — ensure all models registered with Base
from cms.services.scheduler import scheduler_loop
from cms.services.storage import (
    AzureStorageBackend,
    LocalStorageBackend,
    init_storage,
)
from cms.services.version_checker import version_check_loop
from cms.services.device_purge import device_purge_loop
from cms.services.transcoder import (
    deleted_asset_reaper_loop,
    outbox_drain_loop,
    stream_capture_monitor_loop,
)

logger = logging.getLogger("agora.cms")


async def _seed_profiles(db):
    """Create built-in device profiles if they don't exist.

    Existing built-in profiles are left as-is — admin customizations
    are preserved.  Use POST /api/profiles/{id}/reset to restore defaults.
    """
    from sqlalchemy import select
    from cms.models.device_profile import DeviceProfile
    from cms.profile_defaults import BUILTIN_PROFILES

    existing = await db.execute(select(DeviceProfile.name))
    existing_names = {r[0] for r in existing.all()}

    for name, defaults in BUILTIN_PROFILES.items():
        if name not in existing_names:
            profile = DeviceProfile(name=name, builtin=True, **defaults)
            db.add(profile)
            await db.commit()
            await db.refresh(profile)
            logger.info("Seeded device profile: %s", name)

            # Queue transcoding for any existing video assets
            from cms.services.transcoder import enqueue_for_new_profile, enqueue_variants
            variant_ids = await enqueue_for_new_profile(profile.id, db)
            if variant_ids:
                await enqueue_variants(db, variant_ids)
                logger.info("Enqueued %d variants for new profile %s", len(variant_ids), name)

    # Ensure all video assets have variants for all profiles (handles gaps)
    from cms.services.transcoder import enqueue_for_new_profile, enqueue_variants
    all_profiles = await db.execute(select(DeviceProfile))
    for profile in all_profiles.scalars().all():
        variant_ids = await enqueue_for_new_profile(profile.id, db)
        if variant_ids:
            await enqueue_variants(db, variant_ids)
            logger.info("Enqueued %d missing variants for profile %s", len(variant_ids), profile.name)


async def _backfill_media_metadata(settings):
    """One-shot: probe existing assets/variants that have no metadata yet."""
    from sqlalchemy import select
    from cms.models.asset import Asset, AssetVariant
    from cms.services.transcoder import probe_media

    try:
        async for db in get_db():
            # Backfill source assets
            result = await db.execute(
                select(Asset).where(Asset.width.is_(None))
            )
            assets = result.scalars().all()
            for asset in assets:
                file_path = settings.asset_storage_path / asset.filename
                if not file_path.is_file():
                    continue
                meta = await probe_media(file_path)
                for key, val in meta.items():
                    if val is not None:
                        setattr(asset, key, val)
            if assets:
                await db.commit()
                logger.info("Backfilled metadata for %d assets", len(assets))

            # Backfill variants
            result = await db.execute(
                select(AssetVariant).where(
                    AssetVariant.width.is_(None),
                    AssetVariant.status == "ready",
                )
            )
            variants = result.scalars().all()
            for variant in variants:
                file_path = settings.asset_storage_path / "variants" / variant.filename
                if not file_path.is_file():
                    continue
                meta = await probe_media(file_path)
                for key, val in meta.items():
                    if val is not None:
                        setattr(variant, key, val)
            if variants:
                await db.commit()
                logger.info("Backfilled metadata for %d variants", len(variants))
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Metadata backfill failed: %s", e)


async def _seed_roles(db):
    """Create built-in RBAC roles if they don't exist, and sync their permissions."""
    from sqlalchemy import select
    from cms.models.user import Role
    from cms.permissions import BUILTIN_ROLES

    for name, spec in BUILTIN_ROLES.items():
        result = await db.execute(select(Role).where(Role.name == name))
        existing = result.scalar_one_or_none()
        if existing is None:
            db.add(Role(
                name=name,
                description=spec["description"],
                permissions=spec["permissions"],
                is_builtin=True,
            ))
            logger.info("Seeded built-in role: %s", name)
        else:
            # Sync permissions for built-in roles to handle upgrades
            if set(existing.permissions) != set(spec["permissions"]):
                existing.permissions = spec["permissions"]
                logger.info("Updated permissions for built-in role: %s", name)
            if existing.description != spec["description"]:
                existing.description = spec["description"]
    await db.commit()


async def service_key_rotation_loop() -> None:
    """Background loop that auto-rotates the MCP service key every hour."""
    from cms.auth import (
        SETTING_MCP_ENABLED,
        SETTING_MCP_SERVICE_KEY_HASH,
        get_setting,
        get_settings,
        provision_service_key,
    )
    from cms.database import get_db
    from cms.mcp_utils import notify_mcp_reload

    # Wait for startup
    try:
        await asyncio.sleep(60)
    except asyncio.CancelledError:
        return

    while True:
        try:
            settings = get_settings()
            async for db in get_db():
                enabled = await get_setting(db, SETTING_MCP_ENABLED)
                has_key = await get_setting(db, SETTING_MCP_SERVICE_KEY_HASH)
                if enabled == "true" and has_key:
                    await provision_service_key(
                        db, settings.service_key_path,
                        keyvault_uri=settings.azure_keyvault_uri,
                    )
                    await notify_mcp_reload(settings)
                    logger.info("MCP service key rotated")
        except asyncio.CancelledError:
            return
        except Exception:
            logger.exception("Error in service key rotation loop")

        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            return


async def _alert_settings_refresh_loop() -> None:
    """Periodically refresh alert thresholds from the database."""
    from cms.services.alert_service import alert_service

    while True:
        try:
            await alert_service.refresh_settings()
        except asyncio.CancelledError:
            return
        except Exception:
            logger.debug("Alert settings refresh failed")
        try:
            await asyncio.sleep(300)  # Refresh every 5 minutes
        except asyncio.CancelledError:
            return


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    settings = get_settings()
    # Detect and warn on default secrets before anything else talks to the
    # network — this lands the warning at the top of the log where
    # operators will actually see it. See cms/security.py for the list.
    from cms.security import warn_on_default_secrets
    warn_on_default_secrets(settings, logger)
    init_db(settings)
    await wait_for_db()
    await run_migrations()
    settings.asset_storage_path.mkdir(parents=True, exist_ok=True)

    # Initialize storage backend
    if settings.storage_backend == "azure":
        if not settings.azure_storage_connection_string:
            raise RuntimeError(
                "AGORA_CMS_AZURE_STORAGE_CONNECTION_STRING is required "
                "when storage_backend is 'azure'"
            )
        backend = AzureStorageBackend(
            base_path=settings.asset_storage_path,
            connection_string=settings.azure_storage_connection_string,
            account_name=settings.azure_storage_account_name,
            account_key=settings.azure_storage_account_key,
            sas_expiry_hours=settings.azure_sas_expiry_hours,
        )
    else:
        backend = LocalStorageBackend(base_path=settings.asset_storage_path)
    init_storage(backend)

    # Seed built-in RBAC roles (Admin, Operator, Viewer)
    async for db in get_db():
        await _seed_roles(db)

    # Auto-mark setup as completed for existing deployments that are
    # upgrading from a version before the setup wizard was added.
    # Must run BEFORE ensure_admin_credentials so we can distinguish
    # "admin already existed" (upgrade) from "no admin yet" (fresh).
    async for db in get_db():
        from cms.auth import get_setting, set_setting, SETTING_SETUP_COMPLETED
        completed = await get_setting(db, SETTING_SETUP_COMPLETED)
        if completed is None:
            from sqlalchemy import select as _sel
            from cms.models.user import User
            result = await db.execute(
                _sel(User).where(
                    User.username == settings.admin_username,
                    User.is_active.is_(True),
                ).limit(1)
            )
            if result.scalar_one_or_none() is not None:
                await set_setting(db, SETTING_SETUP_COMPLETED, "true")
                await db.commit()
                logger.info("Existing deployment detected — setup wizard skipped")

    # Seed admin credentials from env vars if not already in DB
    async for db in get_db():
        await ensure_admin_credentials(db, settings)

    # Seed built-in device profiles
    async for db in get_db():
        await _seed_profiles(db)

    # Fix image variants that were incorrectly created with .mp4 extensions
    async for db in get_db():
        from cms.services.transcoder import fix_image_variant_extensions
        await fix_image_variant_extensions(db)

    # Start background tasks (transcoding is handled by the dedicated worker container)
    scheduler_task = asyncio.create_task(scheduler_loop())
    backfill_task = asyncio.create_task(_backfill_media_metadata(settings))
    version_check_task = asyncio.create_task(version_check_loop())
    device_purge_task = asyncio.create_task(device_purge_loop())
    key_rotation_task = asyncio.create_task(service_key_rotation_loop())
    alert_refresh_task = asyncio.create_task(_alert_settings_refresh_loop())
    capture_monitor_task = asyncio.create_task(stream_capture_monitor_loop())
    reaper_task = asyncio.create_task(deleted_asset_reaper_loop())
    outbox_drain_task = asyncio.create_task(outbox_drain_loop())

    # Log CMS startup to the event log (so upgrades/restarts show up in the timeline)
    try:
        from cms.models.device_event import DeviceEvent, DeviceEventType
        async for db in get_db():
            db.add(DeviceEvent(
                device_id=None,
                device_name="CMS",
                group_id=None,
                group_name="",
                event_type=DeviceEventType.CMS_STARTED,
                details={"version": __version__},
            ))
            await db.commit()
            break
    except Exception:
        logger.exception("Failed to log CMS_STARTED event")

    logger.info("Agora CMS %s started", __version__)
    yield
    # Shutdown — log CMS shutdown first so the event is persisted before tasks stop
    try:
        from cms.models.device_event import DeviceEvent, DeviceEventType
        async for db in get_db():
            db.add(DeviceEvent(
                device_id=None,
                device_name="CMS",
                group_id=None,
                group_name="",
                event_type=DeviceEventType.CMS_STOPPED,
                details={"version": __version__},
            ))
            await db.commit()
            break
    except Exception:
        logger.exception("Failed to log CMS_STOPPED event")

    scheduler_task.cancel()
    backfill_task.cancel()
    version_check_task.cancel()
    device_purge_task.cancel()
    key_rotation_task.cancel()
    alert_refresh_task.cancel()
    capture_monitor_task.cancel()
    reaper_task.cancel()
    outbox_drain_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    try:
        await backfill_task
    except asyncio.CancelledError:
        pass
    try:
        await version_check_task
    except asyncio.CancelledError:
        pass
    try:
        await device_purge_task
    except asyncio.CancelledError:
        pass
    try:
        await key_rotation_task
    except asyncio.CancelledError:
        pass
    try:
        await alert_refresh_task
    except asyncio.CancelledError:
        pass
    try:
        await capture_monitor_task
    except asyncio.CancelledError:
        pass
    try:
        await reaper_task
    except asyncio.CancelledError:
        pass
    try:
        await outbox_drain_task
    except asyncio.CancelledError:
        pass
    # Close storage backend (Azure: close async blob client)
    if hasattr(backend, "close"):
        await backend.close()
    await dispose_db()


app = FastAPI(
    title="Agora CMS",
    description="Central management system for Agora media playback devices",
    version=__version__,
    lifespan=lifespan,
)


# ---------------------------------------------------------------------------
# Setup-wizard redirect middleware
# ---------------------------------------------------------------------------
_SETUP_ALLOWED_PREFIXES = ("/setup", "/static", "/healthz", "/api/devices/ws", "/login")

# Cache the setup status in-memory to avoid DB queries on every request.
# Set to True once first-run wizard is completed; reset on app restart.
_setup_completed_cache: bool | None = None


async def _is_setup_completed(db: AsyncSession) -> bool:
    """Return True when the first-run setup wizard has been completed."""
    global _setup_completed_cache  # noqa: PLW0603
    if _setup_completed_cache is True:
        return True
    from cms.auth import get_setting, SETTING_SETUP_COMPLETED
    val = await get_setting(db, SETTING_SETUP_COMPLETED)
    if val == "true":
        _setup_completed_cache = True
        return True
    return False


@app.middleware("http")
async def setup_redirect_middleware(request: Request, call_next):
    """Redirect every request to /setup until the first-run wizard is done."""
    path = request.url.path
    if not any(path.startswith(p) for p in _SETUP_ALLOWED_PREFIXES):
        if _setup_completed_cache is not True:
            from cms.database import get_session_factory
            _sf = get_session_factory()
            if _sf is not None:
                async with _sf() as db:
                    if not await _is_setup_completed(db):
                        accept = request.headers.get("accept", "")
                        if "text/html" in accept:
                            return RedirectResponse(url="/setup", status_code=303)
                        return JSONResponse(
                            status_code=503,
                            content={"detail": "First-run setup has not been completed."},
                        )
    return await call_next(request)


@app.exception_handler(HTTPException)
async def unauthorized_redirect(request: Request, exc: HTTPException):
    """Redirect browser requests to /login on 401, return JSON for API calls."""
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        accept = request.headers.get("accept", "")
        if "text/html" in accept:
            return RedirectResponse(url="/login", status_code=303)
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=exc.status_code,
        content={"detail": exc.detail},
    )

# Static files
app.mount("/static", StaticFiles(directory="cms/static"), name="static")

# API routes
from cms.routers.assets import device_router as assets_device_router  # noqa: E402
from cms.routers.assets import router as assets_router  # noqa: E402
from cms.routers.devices import router as devices_router  # noqa: E402
from cms.routers.logs import router as logs_router  # noqa: E402
from cms.routers.mcp import router as mcp_router  # noqa: E402
from cms.routers.profiles import router as profiles_router  # noqa: E402
from cms.routers.schedules import router as schedules_router  # noqa: E402
from cms.routers.ws import router as ws_router  # noqa: E402
from cms.routers.api_keys import router as api_keys_router  # noqa: E402
from cms.routers.audit import router as audit_router  # noqa: E402
from cms.routers.notifications import router as notifications_router  # noqa: E402
from cms.routers.notification_prefs import router as notification_prefs_router  # noqa: E402
from cms.routers.device_events import router as device_events_router  # noqa: E402
from cms.routers.roles import router as roles_router  # noqa: E402
from cms.routers.stream_probe import router as stream_probe_router  # noqa: E402
from cms.routers.users import router as users_router  # noqa: E402
from cms.ui import router as ui_router  # noqa: E402

app.include_router(devices_router)
app.include_router(assets_router)
app.include_router(assets_device_router)
app.include_router(schedules_router)
app.include_router(profiles_router)
app.include_router(logs_router)
app.include_router(mcp_router)
app.include_router(api_keys_router)
app.include_router(audit_router)
app.include_router(notifications_router)
app.include_router(notification_prefs_router)
app.include_router(device_events_router)
app.include_router(users_router)
app.include_router(roles_router)
app.include_router(stream_probe_router)
app.include_router(ws_router)
app.include_router(ui_router)


@app.get("/healthz", tags=["system"])
async def healthz(db: AsyncSession = Depends(get_db)):
    """Lightweight liveness probe — verifies the app can reach the database."""
    from sqlalchemy import text
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "version": __version__}


@app.get("/healthz/system", tags=["system"])
async def healthz_system(db: AsyncSession = Depends(get_db)):
    """Unauthenticated aggregated health probe for post-deploy smoke tests.

    Returns boolean status for each subsystem (db, mcp, smtp) plus the
    deployed version, without exposing any sensitive configuration.  Intended
    for CI/monitoring callers that cannot authenticate.  The authenticated
    UI equivalent ``/api/system/health`` returns richer detail for humans.
    """
    from sqlalchemy import text
    import httpx
    from cms.auth import (
        SETTING_MCP_ENABLED,
        SETTING_SMTP_HOST,
        get_setting,
    )

    db_ok = False
    try:
        await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass

    smtp_host = ""
    try:
        smtp_host = (await get_setting(db, SETTING_SMTP_HOST)) or ""
    except Exception:
        pass
    smtp_configured = bool(smtp_host.strip())

    mcp_enabled = False
    mcp_online = False
    try:
        mcp_enabled = (await get_setting(db, SETTING_MCP_ENABLED)) == "true"
    except Exception:
        pass
    if mcp_enabled:
        try:
            settings = get_settings()
            mcp_url = settings.mcp_server_url.rstrip("/")
            async with httpx.AsyncClient(timeout=5.0) as client:
                resp = await client.get(f"{mcp_url}/health")
                mcp_online = resp.status_code == 200
        except Exception:
            mcp_online = False

    overall_ok = db_ok and (not mcp_enabled or mcp_online)
    return {
        "status": "ok" if overall_ok else "degraded",
        "version": __version__,
        "db": {"ok": db_ok},
        "mcp": {"enabled": mcp_enabled, "ok": mcp_online},
        "smtp": {"configured": smtp_configured},
    }
