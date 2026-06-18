"""Alembic migration 0045 — tag-mode slideshows (agora-cms#806).

Creates ``slideshow_tag_rules``: a 1:1 side table on a SLIDESHOW asset
that switches it into *tag mode*.  A tag-mode slideshow has no
``slideshow_slides`` rows; its deck is resolved live from the set of
assets currently carrying ``tag_id`` (ordered by tag-membership creation
time).  The presence of a row here is the mode switch the resolver keys
off of.

``anchor_at`` persists the wall-clock cycle anchor so newly-tagged
assets can be appended at the tail of a *running* slideshow without
re-flooring the anchor — the firmware player derives the on-screen slide
as ``(now - started_at) % cycle_duration``, so a fixed anchor + tail-only
appends keep every existing slide's offset unchanged (no restart/jump).

The ``default_*`` columns carry the deck-level per-slide playback
defaults (a tag deck has no per-slide authoring UI); they mirror
``slideshow_slides`` column defaults so a tag deck behaves like a manual
deck of default slides.

SQLite (unit-test harness) can't ALTER TABLE ADD CONSTRAINT, but
``create_table`` emits inline FKs/CHECKs fine on both dialects, so the
table is created identically.  We only branch for the GIN/extension-free
nature of this table (there is none) — no Postgres-only DDL here.

Revision ID: 0045
Revises: 0044
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0045"
down_revision = "0044"
branch_labels = None
depends_on = None

# Frozen copy of cms.models.slideshow_tag_rule.DEFAULT_TAG_SLIDE_DURATION_MS
# — migrations must be self-contained and must not import live app code.
_DEFAULT_DURATION_MS = 8000


def _uuid_type(bind):
    """UUID column type matching the dialect (Postgres in prod, SQLite in tests)."""
    if bind.dialect.name == "postgresql":
        return sa.dialects.postgresql.UUID(as_uuid=True)
    return sa.String(length=36)


def upgrade() -> None:
    bind = op.get_bind()
    uuid_t = _uuid_type(bind)

    op.create_table(
        "slideshow_tag_rules",
        sa.Column("slideshow_asset_id", uuid_t, nullable=False),
        sa.Column("tag_id", uuid_t, nullable=False),
        sa.Column(
            "order_by",
            sa.String(length=32),
            nullable=False,
            server_default="tagged_at",
        ),
        sa.Column(
            "default_duration_ms",
            sa.Integer(),
            nullable=False,
            server_default=str(_DEFAULT_DURATION_MS),
        ),
        sa.Column(
            "default_transition",
            sa.String(length=16),
            nullable=False,
            server_default="cut",
        ),
        sa.Column(
            "default_transition_ms",
            sa.Integer(),
            nullable=False,
            server_default="600",
        ),
        sa.Column(
            "default_fit",
            sa.String(length=16),
            nullable=False,
            server_default="cover",
        ),
        sa.Column(
            "default_effect",
            sa.String(length=16),
            nullable=False,
            server_default="none",
        ),
        sa.Column(
            "default_effect_direction",
            sa.String(length=16),
            nullable=False,
            server_default="in",
        ),
        sa.Column("anchor_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.current_timestamp(),
        ),
        sa.PrimaryKeyConstraint(
            "slideshow_asset_id", name="pk_slideshow_tag_rules"
        ),
        sa.ForeignKeyConstraint(
            ["slideshow_asset_id"],
            ["assets.id"],
            name="fk_slideshow_tag_rules_asset",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tag_id"],
            ["tags.id"],
            name="fk_slideshow_tag_rules_tag",
            ondelete="CASCADE",
        ),
        sa.CheckConstraint(
            "order_by IN ('tagged_at')",
            name="ck_slideshow_tag_rule_order_by_known",
        ),
        sa.CheckConstraint(
            "default_duration_ms > 0",
            name="ck_slideshow_tag_rule_duration_pos",
        ),
    )
    op.create_index(
        "ix_slideshow_tag_rules_tag_id",
        "slideshow_tag_rules",
        ["tag_id"],
    )


def downgrade() -> None:
    raise NotImplementedError("downgrade of 0045 is not supported")
