"""Add the feature_flags table.

Backs the feature-flag system (``cms.services.feature_flags``), which lets
"deployed to production" and "released to users" be separate decisions:
work ships dark and is turned on per user or per role from the CMS, without a
redeploy.

Only mutable state is stored here.  A flag's meaning — description, owner,
default, expiry — is declared in code in the registry, so this table carries
no rows on a fresh environment and every flag falls back to its declared
default until an admin changes it.

``user_ids``/``role_ids`` are JSON lists rather than join tables: they are read
whole, never joined against, and hold a handful of pilot users per flag.

Revision ID: 0061
Revises: 0060
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "0061"
down_revision = "0060"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "feature_flags",
        # The registry key is the identity; no surrogate id, so the table is
        # readable when inspected directly during an incident.
        sa.Column("name", sa.String(length=100), primary_key=True),
        sa.Column("state", sa.String(length=16), nullable=False),
        sa.Column(
            "user_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "role_ids",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="[]",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        # SET NULL, not CASCADE: deleting the admin who flipped a flag must not
        # delete the flag with them.
        sa.Column(
            "updated_by_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade is not supported: dropping feature_flags would discard "
        "every flag's state and targeting, silently reverting features to "
        "their code defaults for everyone."
    )
