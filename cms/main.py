"""Agora CMS — FastAPI application entry point."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from cms import __version__
from cms.auth import get_settings
from cms.database import create_tables, dispose_db, init_db

logger = logging.getLogger("agora.cms")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    settings = get_settings()
    init_db(settings)
    await create_tables()
    settings.asset_storage_path.mkdir(parents=True, exist_ok=True)
    logger.info("Agora CMS %s started", __version__)
    yield
    # Shutdown
    await dispose_db()


app = FastAPI(
    title="Agora CMS",
    description="Central management system for Agora media playback devices",
    version=__version__,
    lifespan=lifespan,
)

# Static files
app.mount("/static", StaticFiles(directory="cms/static"), name="static")

# API routes
from cms.routers.assets import router as assets_router  # noqa: E402
from cms.routers.devices import router as devices_router  # noqa: E402
from cms.routers.schedules import router as schedules_router  # noqa: E402
from cms.routers.ws import router as ws_router  # noqa: E402
from cms.ui import router as ui_router  # noqa: E402

app.include_router(devices_router)
app.include_router(assets_router)
app.include_router(schedules_router)
app.include_router(ws_router)
app.include_router(ui_router)
