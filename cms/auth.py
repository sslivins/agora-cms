"""Authentication — session-based for web UI."""

from functools import lru_cache

from fastapi import Depends, HTTPException, Request, status
from itsdangerous import BadSignature, URLSafeTimedSerializer

from cms.config import Settings

COOKIE_NAME = "agora_cms_session"
MAX_AGE = 86400  # 24 hours


@lru_cache
def get_settings() -> Settings:
    return Settings()


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
