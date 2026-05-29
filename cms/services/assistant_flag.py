"""Per-user feature flag for the in-CMS Assistant.

Phase 1 uses the existing ``cms_settings`` key/value table to hold an
allowlist of user UUIDs that can see and use the feature:

* Setting key:  ``assistant_enabled_user_ids``
* Setting value: JSON-encoded list of UUID strings, e.g.
  ``["6a9e2c1e-...", "ee9b4d2a-..."]``.
* Missing setting / empty list  → feature is OFF for everyone (the
  default state on every freshly-deployed env).

Rationale for reusing ``cms_settings`` instead of adding a generic
feature-flag framework:  we only have one flag right now, and the
existing settings table already handles persistence, admin gating, and
the migration story.  If we grow to >3 flags or need
per-org / per-group scoping, promote this to a real table at that
point.

The admin user (``users.is_active`` AND role permission
``settings:write``) is treated as always enabled.  This is the
escape hatch so we can't accidentally lock ourselves out of the
feature.
"""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_setting, set_setting
from cms.models.user import User
from cms.permissions import has_permission


logger = logging.getLogger(__name__)


ASSISTANT_FLAG_KEY = "assistant_enabled_user_ids"
# Permission that always implies access regardless of allowlist
# membership — keeps admins from locking themselves out.
ADMIN_FALLBACK_PERMISSION = "settings:write"


async def get_allowlist(db: AsyncSession) -> list[uuid.UUID]:
    """Return the current allowlist as a list of UUIDs.

    Tolerates a missing setting, an empty string, malformed JSON, and
    non-UUID entries — any of those collapse to an empty list and log
    a warning.  We never want a bad setting value to throw 500s into
    a router.
    """
    raw = await get_setting(db, ASSISTANT_FLAG_KEY)
    if not raw:
        return []
    try:
        items = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning(
            "assistant_enabled_user_ids contains invalid JSON; treating as empty"
        )
        return []
    if not isinstance(items, list):
        logger.warning(
            "assistant_enabled_user_ids is not a JSON list; treating as empty"
        )
        return []
    out: list[uuid.UUID] = []
    for item in items:
        try:
            out.append(uuid.UUID(str(item)))
        except (ValueError, TypeError):
            logger.warning(
                "assistant_enabled_user_ids entry %r is not a valid UUID; ignored",
                item,
            )
    return out


async def assistant_enabled_for(db: AsyncSession, user: User) -> bool:
    """Return True if ``user`` is allowed to use the Assistant feature.

    The check is intentionally permissive for ``settings:write`` holders
    (admins) — they always have access, even when the allowlist is empty,
    so a bad allowlist setting can never lock everyone out of the
    feature including the people who can fix it.
    """
    if user.role and has_permission(
        user.role.permissions, ADMIN_FALLBACK_PERMISSION
    ):
        return True
    allowlist = await get_allowlist(db)
    return user.id in allowlist


async def set_allowlist(db: AsyncSession, user_ids: list[uuid.UUID]) -> None:
    """Replace the allowlist with ``user_ids``.

    Deduplicates while preserving caller order; persists as a JSON
    array of lowercase canonical UUID strings.
    """
    seen: set[uuid.UUID] = set()
    cleaned: list[str] = []
    for uid in user_ids:
        if uid in seen:
            continue
        seen.add(uid)
        cleaned.append(str(uid))
    await set_setting(db, ASSISTANT_FLAG_KEY, json.dumps(cleaned))
