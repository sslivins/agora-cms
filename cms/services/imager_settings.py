"""Imager settings persisted in the ``cms_settings`` key/value table.

Currently only ``imager.catalog_url`` lives here, but the module is
written generically so future imager-related runtime settings can be
added without proliferating tables.

Storing the catalog URL in the DB (instead of an env var) lets a
tenant admin point their CMS at a different upstream catalog from the
UI without a redeploy.  The deploy-time env var was removed in PR 7.
"""

from __future__ import annotations

from urllib.parse import urlparse

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.setting import CMSSetting
from shared.services.imager_catalog import parse_allowed_hosts


CATALOG_URL_KEY = "imager.catalog_url"


class CatalogUrlValidationError(ValueError):
    """Raised when a candidate catalog URL fails validation.

    Carries a single ``message`` attribute suitable for surfacing
    directly to the admin via an HTTP 422 response.
    """


def validate_catalog_url(url: str, allowed_hosts_raw: str) -> str:
    """Return ``url`` (stripped) iff scheme is https and host allowlisted.

    The URL must be ``https://`` and its hostname must appear in the
    deploy-time ``base_image_allowed_hosts`` list.  We deliberately
    keep the allowlist as a deployment guard rail (not user-tunable)
    even though the URL itself is now user-tunable -- it limits the
    blast radius if an admin account is compromised.
    """
    candidate = (url or "").strip()
    if not candidate:
        raise CatalogUrlValidationError("catalog URL must not be empty")
    parsed = urlparse(candidate)
    if parsed.scheme != "https":
        raise CatalogUrlValidationError(
            f"catalog URL must use https (got scheme={parsed.scheme!r})"
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise CatalogUrlValidationError("catalog URL has no hostname")
    allowlist = parse_allowed_hosts(allowed_hosts_raw)
    if not allowlist:
        raise CatalogUrlValidationError(
            "BASE_IMAGE_ALLOWED_HOSTS is empty; refusing to accept any URL"
        )
    if host not in allowlist:
        raise CatalogUrlValidationError(
            f"host {host!r} is not in BASE_IMAGE_ALLOWED_HOSTS ({sorted(allowlist)})"
        )
    return candidate


async def get_catalog_url(db: AsyncSession) -> str | None:
    """Return the configured catalog URL, or ``None`` if unset."""
    result = await db.execute(
        select(CMSSetting.value).where(CMSSetting.key == CATALOG_URL_KEY)
    )
    value = result.scalar_one_or_none()
    if value is None:
        return None
    value = value.strip()
    return value or None


async def set_catalog_url(db: AsyncSession, url: str) -> str:
    """Upsert the catalog URL.  Returns the stored (stripped) value.

    Caller is responsible for validation (call :func:`validate_catalog_url`
    first) and for committing the session.  Empty / whitespace-only
    values clear the setting.
    """
    cleaned = (url or "").strip()
    result = await db.execute(
        select(CMSSetting).where(CMSSetting.key == CATALOG_URL_KEY)
    )
    row = result.scalar_one_or_none()
    if row is None:
        db.add(CMSSetting(key=CATALOG_URL_KEY, value=cleaned))
    else:
        row.value = cleaned
    return cleaned


async def clear_catalog_url(db: AsyncSession) -> None:
    """Clear the catalog URL setting (sets value to empty string)."""
    await set_catalog_url(db, "")
