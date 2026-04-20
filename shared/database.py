"""Database engine and session management (shared between CMS and worker)."""

import asyncio
import logging

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from shared.config import SharedSettings

logger = logging.getLogger("agora.database")


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory = None


def init_db(settings: SharedSettings):
    global _engine, _session_factory
    # ``pool_pre_ping`` issues a cheap "SELECT 1" before handing out a pooled
    # connection; this catches connections invalidated by a Postgres restart,
    # idle-timeout disconnect, network blip, or Azure managed-DB failover.
    # Without it, the next checkout after such an event raises
    # ``InterfaceError: connection is closed`` and (in the worker's listen
    # loop) crashed the entire process — see incident write-up in the
    # accompanying PR. ``pool_recycle`` proactively retires connections older
    # than 5 minutes so we don't accumulate ones the server has already
    # closed silently.
    _engine = create_async_engine(
        settings.database_url,
        echo=False,
        pool_pre_ping=True,
        pool_recycle=300,
    )
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


async def wait_for_db(max_retries: int = 30, base_delay: float = 2.0):
    """Wait for the database to become reachable (retries with backoff).

    Needed in Azure Container Apps where private DNS for PostgreSQL
    may not be propagated when the container first starts.
    """
    for attempt in range(1, max_retries + 1):
        try:
            async with _engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            logger.info("Database connection ready (attempt %d)", attempt)
            return
        except Exception as exc:
            delay = min(base_delay * attempt, 30.0)
            logger.warning(
                "Database not ready (attempt %d/%d): %s — retrying in %.0fs",
                attempt, max_retries, exc, delay,
            )
            await asyncio.sleep(delay)
    raise RuntimeError(
        f"Database not reachable after {max_retries} attempts"
    )


def get_engine():
    """Return the current async engine (for raw connection access)."""
    return _engine


def get_session_factory():
    """Return the current session factory."""
    return _session_factory


async def dispose_db():
    global _engine
    if _engine:
        await _engine.dispose()


async def get_db() -> AsyncSession:
    async with _session_factory() as session:
        yield session


async def create_tables():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
