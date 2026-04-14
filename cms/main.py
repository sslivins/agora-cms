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
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.ext.asyncio import AsyncSession

from cms import __version__
from cms.auth import ensure_admin_credentials, get_settings
from cms.database import create_tables, dispose_db, get_db, init_db, run_migrations
from cms.models import *  # noqa: F401,F403 — ensure all models registered with Base
from cms.services.scheduler import scheduler_loop
from cms.services.storage import (
    AzureStorageBackend,
    LocalStorageBackend,
    init_storage,
)
from cms.services.version_checker import version_check_loop
from cms.services.device_purge import device_purge_loop

logger = logging.getLogger("agora.cms")


async def _seed_profiles(db):
    """Create built-in device profiles if they don't exist."""
    from sqlalchemy import select
    from cms.models.device_profile import DeviceProfile

    existing = await db.execute(select(DeviceProfile.name))
    existing_names = {r[0] for r in existing.all()}

    defaults = [
        {
            "name": "pi-zero-2w",
            "description": "Raspberry Pi Zero 2 W — H.264 Main, 1080p30",
            "video_codec": "h264",
            "video_profile": "main",
            "max_width": 1920,
            "max_height": 1080,
            "max_fps": 30,
            "crf": 23,
            "audio_codec": "aac",
            "audio_bitrate": "128k",
            "builtin": True,
        },
        {
            "name": "pi-4",
            "description": "Raspberry Pi 4 — HEVC Main, 1080p30",
            "video_codec": "h265",
            "video_profile": "main",
            "max_width": 1920,
            "max_height": 1080,
            "max_fps": 30,
            "crf": 23,
            "audio_codec": "aac",
            "audio_bitrate": "128k",
            "builtin": True,
        },
        {
            "name": "pi-5",
            "description": "Raspberry Pi 5 / CM5 — HEVC Main, 1080p60",
            "video_codec": "h265",
            "video_profile": "main",
            "max_width": 1920,
            "max_height": 1080,
            "max_fps": 60,
            "crf": 23,
            "audio_codec": "aac",
            "audio_bitrate": "128k",
            "builtin": True,
        },
    ]

    for d in defaults:
        if d["name"] not in existing_names:
            profile = DeviceProfile(**d)
            db.add(profile)
            await db.commit()
            await db.refresh(profile)
            logger.info("Seeded device profile: %s", d["name"])

            # Queue transcoding for any existing video assets
            from cms.services.transcoder import enqueue_for_new_profile
            count = await enqueue_for_new_profile(profile.id, db)
            if count:
                logger.info("Enqueued %d variants for new profile %s", count, d["name"])
        else:
            # Reset built-in profile to canonical defaults
            result = await db.execute(
                select(DeviceProfile).where(DeviceProfile.name == d["name"])
            )
            profile = result.scalar_one()
            changed = False
            for field, value in d.items():
                if field == "name":
                    continue
                if getattr(profile, field) != value:
                    setattr(profile, field, value)
                    changed = True
            if changed:
                await db.commit()
                logger.info("Reset built-in profile to defaults: %s", d["name"])

    # Ensure all video assets have variants for all profiles (handles gaps)
    from cms.services.transcoder import enqueue_for_new_profile
    all_profiles = await db.execute(select(DeviceProfile))
    for profile in all_profiles.scalars().all():
        count = await enqueue_for_new_profile(profile.id, db)
        if count:
            logger.info("Enqueued %d missing variants for profile %s", count, profile.name)


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


async def _migrate_variant_filenames(db, settings):
    """One-shot migration: rename legacy variant files to UUID-based names.

    Old scheme: {source_stem}_{profile_name}.mp4
    New scheme: {variant_uuid}.mp4

    Renames files on disk and updates the DB filename column.  Skips variants
    whose filename already looks like a UUID.
    """
    import re
    from sqlalchemy import select
    from cms.models.asset import AssetVariant

    _UUID_FILENAME = re.compile(
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\.mp4$"
    )

    try:
        result = await db.execute(select(AssetVariant))
        variants = result.scalars().all()
        variants_dir = settings.asset_storage_path / "variants"
        migrated = 0

        for v in variants:
            if _UUID_FILENAME.match(v.filename):
                continue  # already migrated

            new_filename = f"{v.id}.mp4"
            old_path = variants_dir / v.filename
            new_path = variants_dir / new_filename

            if old_path.is_file():
                old_path.rename(new_path)

            v.filename = new_filename
            migrated += 1

        if migrated:
            await db.commit()
            logger.info("Migrated %d variant filename(s) to UUID scheme", migrated)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        logger.warning("Variant filename migration failed: %s", e)


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    settings = get_settings()
    init_db(settings)
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

    # Seed admin credentials from env vars if not already in DB
    async for db in get_db():
        await ensure_admin_credentials(db, settings)

    # Seed built-in device profiles
    async for db in get_db():
        await _seed_profiles(db)

    # Migrate variant files from legacy names to UUID-based names
    async for db in get_db():
        await _migrate_variant_filenames(db, settings)

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

    logger.info("Agora CMS %s started", __version__)
    yield
    # Shutdown
    scheduler_task.cancel()
    backfill_task.cancel()
    version_check_task.cancel()
    device_purge_task.cancel()
    key_rotation_task.cancel()
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
from cms.routers.roles import router as roles_router  # noqa: E402
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
app.include_router(users_router)
app.include_router(roles_router)
app.include_router(ws_router)
app.include_router(ui_router)


@app.get("/healthz", tags=["system"])
async def healthz(db: AsyncSession = Depends(get_db)):
    """Lightweight liveness probe — verifies the app can reach the database."""
    from sqlalchemy import text
    await db.execute(text("SELECT 1"))
    return {"status": "ok", "version": __version__}
