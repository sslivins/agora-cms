"""Authentication — session-based for web UI."""

from functools import lru_cache

import bcrypt
from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.config import Settings
from cms.database import get_db
from cms.models.setting import CMSSetting

COOKIE_NAME = "agora_cms_session"
MAX_AGE = 604800  # 7 days

SETTING_PASSWORD_HASH = "admin_password_hash"
SETTING_USERNAME = "admin_username"
SETTING_TIMEZONE = "timezone"


@lru_cache
def get_settings() -> Settings:
    return Settings()


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, hashed: str) -> bool:
    return bcrypt.checkpw(password.encode(), hashed.encode())


def get_serializer(settings: Settings = Depends(get_settings)) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.secret_key)


def require_auth(request: Request, settings: Settings = Depends(get_settings)):
    cookie = request.cookies.get(COOKIE_NAME)
    if not cookie:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)
    serializer = URLSafeTimedSerializer(settings.secret_key)
    try:
        serializer.loads(cookie, max_age=MAX_AGE)
    except BadSignature:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED)


async def get_setting(db: AsyncSession, key: str) -> str | None:
    result = await db.execute(select(CMSSetting).where(CMSSetting.key == key))
    setting = result.scalar_one_or_none()
    return setting.value if setting else None


async def set_setting(db: AsyncSession, key: str, value: str) -> None:
    result = await db.execute(select(CMSSetting).where(CMSSetting.key == key))
    setting = result.scalar_one_or_none()
    if setting:
        setting.value = value
    else:
        db.add(CMSSetting(key=key, value=value))
    await db.commit()


async def ensure_admin_credentials(db: AsyncSession, settings: Settings) -> None:
    """On startup, seed admin credentials from env vars if not already in DB."""
    existing = await get_setting(db, SETTING_PASSWORD_HASH)
    if not existing or settings.reset_password:
        await set_setting(db, SETTING_PASSWORD_HASH, hash_password(settings.admin_password))
        await set_setting(db, SETTING_USERNAME, settings.admin_username)
        if settings.reset_password:
            import logging
            logging.getLogger(__name__).warning("Admin password reset from environment. Set AGORA_CMS_RESET_PASSWORD=false and restart.")
