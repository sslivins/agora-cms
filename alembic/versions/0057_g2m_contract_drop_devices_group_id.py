"""Stage 8b contract: drop legacy ``devices.group_id`` (#863).

Revision ID: 0057
Revises: 0056
"""

from __future__ import annotations

from alembic import op
from sqlalchemy import inspect


revision = "0057"
down_revision = "0056"
branch_labels = None
depends_on = None


def _drop_group_id_indexes() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    for index in inspector.get_indexes("devices"):
        if index.get("column_names") == ["group_id"]:
            op.drop_index(index["name"], table_name="devices")


def _drop_group_id_foreign_keys() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    for fk in inspector.get_foreign_keys("devices"):
        if fk.get("constrained_columns") == ["group_id"]:
            op.drop_constraint(fk["name"], "devices", type_="foreignkey")


def upgrade() -> None:
    _drop_group_id_indexes()
    _drop_group_id_foreign_keys()
    op.drop_column("devices", "group_id")


def downgrade() -> None:
    raise NotImplementedError(
        "Stage 8 contract migration is forward-only; recovery is via forward repair, not downgrade. See #863."
    )
