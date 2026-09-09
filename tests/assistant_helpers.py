"""Test helper for granting users access to the Assistant.

The Assistant used to own a bespoke allowlist in ``cms_settings``; it is now
an ordinary entry in the feature-flag registry.  Tests that just need "these
users can see the Assistant" go through here so the storage detail stays in
one place, and so this file is the only thing to change if the Assistant's
targeting moves again.
"""

from __future__ import annotations

import uuid

from cms.services import feature_flags


async def set_assistant_allowlist(db, user_ids: list[uuid.UUID]) -> None:
    """Target the ``assistant`` flag at exactly ``user_ids`` and commit.

    Mirrors the old ``assistant_flag.set_allowlist`` contract, which committed
    via ``set_setting``: the list is replaced wholesale, and an empty list
    leaves the flag at its declared default (targeted, admins included) rather
    than turning it off.
    """
    await feature_flags.set_state(
        db,
        "assistant",
        state=feature_flags.FlagState.TARGETED,
        user_ids=list(user_ids),
    )
    await db.commit()
