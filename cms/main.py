"""Agora CMS — FastAPI application entry point."""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from cms import __version__


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    yield
    # Shutdown


app = FastAPI(
    title="Agora CMS",
    description="Central management system for Agora media playback devices",
    version=__version__,
    lifespan=lifespan,
)
