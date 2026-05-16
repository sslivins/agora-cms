"""Add ``agora_os_latest_bundle`` single-row table for cross-replica
bundle_checker state (issue agora-cms#578).

Before this revision, ``cms/services/bundle_checker.py`` cached the
"latest agora-os release" in three module-level globals
(``_latest_bundle``, ``_last_success_at``, ``_last_error``).  In a
multi-replica deploy each worker had its own copy, so a
``POST /api/devices/check-updates`` only refreshed the cache of the
one replica it happened to land on; the others kept their stale view
until their own 30-min cron tick fired.  ``GET /api/devices/{id}``
round-robins, so ``update_available`` (computed from
``device.os_version != bundle_checker.latest.target_version``) flipped
True/False from request to request.  Visible UX impact: the "Update"
action in the kebab menu rendered and unrendered every few seconds,
making it noticeably hard to click manually-triggered OTAs.

Solution: persist the bundle in a shared single-row table.  All readers
hit the DB (one PK lookup amortised across all devices in a list response);
all writers UPSERT into the same row.  Multiple replicas writing the same
content is fine — last-write-wins on identical payloads is idempotent.

``_last_error`` deliberately does *not* move here — it's per-replica debug
state that's more useful as-is (lets ops see whether one replica's network
egress is partially broken vs. all of them are failing).  ``_last_success_at``
*does* move because it's the cross-replica freshness signal.

The row is enforced singleton by a ``CHECK (id = 1)`` constraint.  The
column shape mirrors :class:`cms.services.bundle_checker.BundleInfo`.

Revision ID: 0026
Revises: 0025
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agora_os_latest_bundle",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("target_version", sa.Text(), nullable=False),
        sa.Column("release_id", sa.Text(), nullable=False),
        sa.Column("min_from_version", sa.Text(), nullable=False),
        sa.Column("bundle_url", sa.Text(), nullable=False),
        sa.Column("signature_url", sa.Text(), nullable=False),
        sa.Column("sha256_url", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column(
            "last_success_at",
            sa.DateTime(timezone=True),
            nullable=False,
        ),
        sa.CheckConstraint("id = 1", name="ck_agora_os_latest_bundle_single_row"),
    )


def downgrade() -> None:
    # Project policy (test_migration_policy.py): every downgrade must
    # raise NotImplementedError. Forward-only migrations only.
    raise NotImplementedError(
        "Downgrade of 0026 is intentionally not implemented per project policy."
    )
