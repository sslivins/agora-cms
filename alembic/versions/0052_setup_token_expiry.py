"""users: setup_token_created_at column for invite-token expiry

Adds a nullable ``setup_token_created_at`` timestamp to ``users`` so the
welcome/invite magic-login token can carry a TTL (see issue #599 —
"Setup-email tokens never expire"). The application stamps this column
whenever it issues a token (create-user / resend-invite) and the
``/setup-account`` handler rejects tokens older than
``SETUP_TOKEN_TTL`` (7 days).

Backfill: every existing row that still has an outstanding
``setup_token`` (i.e. a pending invitee who never completed setup) is
stamped with the account's own ``created_at``. This makes the TTL apply
*retroactively* from when the account was created — so an invite emailed
weeks ago is treated as already-expired on deploy (exactly the hole #599
describes), while a freshly-created pending invite still works until its
7-day window elapses.

Adding a nullable column with no default is a metadata-only change in
Postgres (brief ACCESS EXCLUSIVE, no table rewrite), and the backfill
UPDATE only touches the handful of rows with a non-null setup_token.

Revision ID: 0052
Revises: 0051
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0052"
down_revision = "0051"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("setup_token_created_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Retroactively date outstanding invite tokens from the account's creation
    # so months-old links expire immediately under the new TTL check.
    op.execute(
        "UPDATE users SET setup_token_created_at = created_at "
        "WHERE setup_token IS NOT NULL AND setup_token_created_at IS NULL"
    )


def downgrade() -> None:
    # Project policy (tests/test_migration_policy.py): downgrades are not
    # supported — forward-only migrations.
    raise NotImplementedError("downgrade not supported")
