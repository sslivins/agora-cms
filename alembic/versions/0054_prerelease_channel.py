"""Add the prerelease opt-in channel: per-channel agora-os bundle table
and ``devices.update_channel``.

Motivation
----------
Before this revision the CMS tracked exactly one "latest agora-os bundle"
(the single-row ``agora_os_latest_bundle`` table, migration 0026) and
offered it to the entire fleet.  ``bundle_checker`` picked the newest
non-draft release *including prereleases*, so publishing a ``-test`` build
made it available to every device — there was no way to test a build in
production on a subset of devices.

This revision splits "latest" into two channels:

* ``stable``      — newest non-draft, non-prerelease release (fleet default).
* ``prerelease``  — newest non-draft release, prereleases included (opt-in).

``devices.update_channel`` records which channel each device follows;
``agora_os_channel_bundle`` holds one bundle row per channel.

Migration shape
---------------
1. Create ``agora_os_channel_bundle`` (PK = channel).  Fresh table rather
   than an in-place ALTER of ``agora_os_latest_bundle`` so we don't have to
   drop the ``CHECK (id = 1)`` constraint — SQLite (used by the local test
   backend) can't drop a named CHECK without a full table rebuild.
2. Seed BOTH channel rows from the existing single row if present.  Today
   every agora-os release is ``prerelease=false``, so the current
   "newest non-draft" row is a valid seed for both channels; the next
   ``bundle_checker`` poll re-derives them correctly.
3. Drop the old ``agora_os_latest_bundle`` table.
4. Add ``devices.update_channel`` defaulting to ``'stable'`` so the entire
   existing fleet stays on the stable channel (no behavioural change until
   a device is explicitly opted in).

Revision ID: 0054
Revises: 0053
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0054"
down_revision = "0053"
branch_labels = None
depends_on = None

_SEED_COLUMNS = (
    "target_version, release_id, min_from_version, bundle_url, "
    "signature_url, sha256_url, size_bytes, created_at, last_success_at"
)


def upgrade() -> None:
    op.create_table(
        "agora_os_channel_bundle",
        sa.Column("channel", sa.Text(), primary_key=True),
        sa.Column("target_version", sa.Text(), nullable=False),
        sa.Column("release_id", sa.Text(), nullable=False),
        sa.Column("min_from_version", sa.Text(), nullable=False),
        sa.Column("bundle_url", sa.Text(), nullable=False),
        sa.Column("signature_url", sa.Text(), nullable=False),
        sa.Column("sha256_url", sa.Text(), nullable=True),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=False),
    )

    # Seed both channels from the old single-row table (if it holds a row).
    for channel in ("stable", "prerelease"):
        op.execute(
            f"INSERT INTO agora_os_channel_bundle "
            f"(channel, {_SEED_COLUMNS}) "
            f"SELECT '{channel}', {_SEED_COLUMNS} "
            f"FROM agora_os_latest_bundle WHERE id = 1"
        )

    op.drop_table("agora_os_latest_bundle")

    op.add_column(
        "devices",
        sa.Column(
            "update_channel",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'stable'"),
        ),
    )


def downgrade() -> None:
    # Project policy (test_migration_policy.py): every downgrade must
    # raise NotImplementedError. Forward-only migrations only.
    raise NotImplementedError(
        "Downgrade of 0054 is intentionally not implemented per project policy."
    )
