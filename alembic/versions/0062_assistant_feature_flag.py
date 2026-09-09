"""Carry the Assistant allowlist onto the feature-flag system.

The Assistant was the prototype for feature flags: an allowlist of user UUIDs
in the ``cms_settings`` key ``assistant_enabled_user_ids``, with anyone holding
``settings:write`` always allowed through as the escape hatch.  That behaviour
now comes from the ``assistant`` entry in the flag registry, which declares
``default=targeted`` and ``include_admins=True`` to reproduce it exactly.

This migration moves the stored data.  It is written to be behaviour-preserving
in every case:

* No setting, empty list, or unparseable value -- no row is written, and the
  registry default (targeted, nobody listed, admins included) applies.  That is
  what the old code did with a missing or broken setting.
* A list of ids -- a ``targeted`` row is written with those ids, so everyone who
  had access keeps it.

Entries that aren't valid UUIDs are skipped rather than failing the migration,
matching the tolerant parsing the old ``get_allowlist`` did: a malformed entry
never granted anyone access, so dropping it changes nothing.

The old ``cms_settings`` row is deliberately left in place. It is no longer
read, but keeping it for a release means the previous allowlist is still
recoverable by hand if this conversion turns out to be wrong -- and this
migration cannot be reversed automatically.

Revision ID: 0062
Revises: 0061
"""

from __future__ import annotations

import json
import uuid

import sqlalchemy as sa
from alembic import op


revision = "0062"
down_revision = "0061"
branch_labels = None
depends_on = None


LEGACY_KEY = "assistant_enabled_user_ids"
FLAG_NAME = "assistant"


def upgrade() -> None:
    conn = op.get_bind()

    raw = conn.execute(
        sa.text("SELECT value FROM cms_settings WHERE key = :k"), {"k": LEGACY_KEY}
    ).scalar()
    if not raw:
        return

    try:
        items = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        # The old reader treated invalid JSON as "nobody allowlisted", so the
        # equivalent here is to write nothing and let the default apply.
        return
    if not isinstance(items, list):
        return

    user_ids: list[str] = []
    for item in items:
        try:
            user_ids.append(str(uuid.UUID(str(item))))
        except (ValueError, TypeError, AttributeError):
            continue
    if not user_ids:
        return

    # Only ids that still resolve to a user: a stale id granted nobody access
    # before, and carrying it over would leave the Features tab showing a
    # phantom entry it cannot render a name for.
    existing = {
        str(r[0])
        for r in conn.execute(
            sa.text("SELECT id FROM users WHERE id = ANY(:ids)"),
            {"ids": [uuid.UUID(u) for u in user_ids]},
        )
    }
    user_ids = [u for u in user_ids if u in existing]
    if not user_ids:
        return

    conn.execute(
        sa.text(
            """
            INSERT INTO feature_flags (name, state, user_ids, role_ids, updated_at)
            VALUES (:name, 'targeted', CAST(:user_ids AS jsonb), '[]'::jsonb, now())
            ON CONFLICT (name) DO NOTHING
            """
        ),
        {"name": FLAG_NAME, "user_ids": json.dumps(user_ids)},
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported: the Assistant allowlist would have to be "
        "reconstructed from the feature_flags row, and any change made since "
        "the upgrade would be silently lost."
    )
