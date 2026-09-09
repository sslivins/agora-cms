"""Access check for the in-CMS Assistant.

This used to be a bespoke flag of its own, storing an allowlist of user UUIDs
in a ``cms_settings`` key.  It was the prototype that the general feature-flag
system generalised, and it now delegates to that system: the ``assistant``
entry in :mod:`cms.services.feature_flags`, administered from the Features tab
alongside every other flag.

The module survives as a one-function shim so the call sites that ask "can this
person use the Assistant?" keep reading naturally, and so there is one obvious
place to look when the answer is surprising.

Behaviour is unchanged from the settings-key version.  The flag declares
``default=targeted`` and ``include_admins=True``, which reproduces the old
semantics exactly -- allowlisted users, plus anyone holding ``settings:write``
as the escape hatch.  Migration 0062 carried the stored allowlist across.
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from cms.models.user import User
from cms.services.feature_flags import enabled

logger = logging.getLogger(__name__)

# The registry key this module wraps.
ASSISTANT_FLAG_KEY = "assistant"

# Where the allowlist used to live.  Nothing reads this setting any more; it is
# named here for the migration and for anyone grepping for the old location.
LEGACY_SETTING_KEY = "assistant_enabled_user_ids"


async def assistant_enabled_for(
    db: AsyncSession, user: User, *, request=None
) -> bool:
    """Return True if ``user`` is allowed to use the Assistant.

    Pass ``request`` where one is available to memoise the answer for that
    request; several call sites check this on a single page load.
    """
    return await enabled(db, ASSISTANT_FLAG_KEY, user, request=request)
