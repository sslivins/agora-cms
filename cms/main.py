"""Agora CMS — FastAPI application entry point."""

import asyncio
import logging
from contextlib import asynccontextmanager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)

from fastapi import FastAPI, Request, status
from fastapi.exceptions import HTTPException
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles

from cms import __version__
from cms.auth import ensure_admin_credentials, get_settings
from cms.database import create_tables, dispose_db, get_db, init_db, run_migrations
from cms.models import *  # noqa: F401,F403 — ensure all models registered with Base
from cms.services.scheduler import scheduler_loop
from cms.services.transcoder import transcoder_loop

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

    # Ensure all video assets have variants for all profiles (handles gaps)
    from cms.services.transcoder import enqueue_for_new_profile
    all_profiles = await db.execute(select(DeviceProfile))
    for profile in all_profiles.scalars().all():
        count = await enqueue_for_new_profile(profile.id, db)
        if count:
            logger.info("Enqueued %d missing variants for profile %s", count, profile.name)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    settings = get_settings()
    init_db(settings)
    await run_migrations()
    settings.asset_storage_path.mkdir(parents=True, exist_ok=True)

    # Seed admin credentials from env vars if not already in DB
    async for db in get_db():
        await ensure_admin_credentials(db, settings)

    # Seed built-in device profiles
    async for db in get_db():
        await _seed_profiles(db)

    # Start background scheduler
    scheduler_task = asyncio.create_task(scheduler_loop())
    transcoder_task = asyncio.create_task(transcoder_loop(settings.asset_storage_path))

    logger.info("Agora CMS %s started", __version__)
    yield
    # Shutdown
    scheduler_task.cancel()
    transcoder_task.cancel()
    try:
        await scheduler_task
    except asyncio.CancelledError:
        pass
    try:
        await transcoder_task
    except asyncio.CancelledError:
        pass
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
from cms.routers.profiles import router as profiles_router  # noqa: E402
from cms.routers.schedules import router as schedules_router  # noqa: E402
from cms.routers.ws import router as ws_router  # noqa: E402
from cms.ui import router as ui_router  # noqa: E402

app.include_router(devices_router)
app.include_router(assets_router)
app.include_router(assets_device_router)
app.include_router(schedules_router)
app.include_router(profiles_router)
app.include_router(ws_router)
app.include_router(ui_router)
