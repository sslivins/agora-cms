"""Database engine and session management."""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from cms.config import Settings


class Base(DeclarativeBase):
    pass


_engine = None
_session_factory = None


def init_db(settings: Settings):
    global _engine, _session_factory
    _engine = create_async_engine(settings.database_url, echo=False)
    _session_factory = async_sessionmaker(_engine, expire_on_commit=False)


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
