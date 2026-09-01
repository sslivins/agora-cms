"""Stage 5: alert lifecycle + notifications/events many-to-many (#863).

Adds:
- ``notification_groups`` for many-to-many group targeting
- ``notification_reads`` for per-user read / dismiss state
- ``device_alerts`` generic lifecycle rows
- ``device_events.group_ids`` + ``group_snapshots`` frozen membership snapshot

Also changes FK behavior so deleting a device preserves historical events
(``device_events.device_id -> ON DELETE SET NULL``) and deleting a group drops
only the legacy scalar pointer on notifications (``notifications.group_id ->
ON DELETE SET NULL``) while the join-row cleanup happens in
``notification_groups``.

Revision ID: 0056
Revises: 0055
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect
from sqlalchemy.dialects import postgresql


revision = "0056"
down_revision = "0055"
branch_labels = None
depends_on = None


def _is_postgres() -> bool:
    return op.get_bind().dialect.name == "postgresql"


def _json_type():
    if _is_postgres():
        return postgresql.JSONB(astext_type=sa.Text())
    return sa.JSON()


def _group_ids_type():
    if _is_postgres():
        return postgresql.ARRAY(postgresql.UUID(as_uuid=True))
    return sa.JSON()


def _drop_fk(table_name: str, column_name: str) -> None:
    bind = op.get_bind()
    for fk in inspect(bind).get_foreign_keys(table_name):
        if fk.get("constrained_columns") == [column_name]:
            op.drop_constraint(fk["name"], table_name, type_="foreignkey")
            return
    raise RuntimeError(f"FK on {table_name}.{column_name} not found")


def upgrade() -> None:
    op.create_table(
        "notification_groups",
        sa.Column(
            "notification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notifications.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "group_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("device_groups.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
    )
    op.create_index(
        "ix_notification_groups_group_notification",
        "notification_groups",
        ["group_id", "notification_id"],
    )

    op.create_table(
        "notification_reads",
        sa.Column(
            "notification_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("notifications.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("dismissed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index(
        "ix_notification_reads_user_dismissed",
        "notification_reads",
        ["user_id", "dismissed_at"],
    )

    op.execute(
        """
        INSERT INTO notification_groups (notification_id, group_id)
        SELECT id, group_id
        FROM notifications
        WHERE scope = 'group' AND group_id IS NOT NULL
        """
    )

    op.create_table(
        "device_alerts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            nullable=False,
        ),
        sa.Column(
            "device_id",
            sa.String(length=64),
            sa.ForeignKey("devices.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False),
        sa.Column("incident_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "raise_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("device_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "resolve_event_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("device_events.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.UniqueConstraint("device_id", "kind", name="uq_device_alerts_device_kind"),
    )
    op.create_index("ix_device_alerts_device_id", "device_alerts", ["device_id"])
    op.create_index("ix_device_alerts_incident_id", "device_alerts", ["incident_id"])
    op.create_index("ix_device_alerts_state_kind", "device_alerts", ["state", "kind"])

    bind = op.get_bind()
    device_alerts = sa.table(
        "device_alerts",
        sa.column("id", postgresql.UUID(as_uuid=True)),
        sa.column("device_id", sa.String()),
        sa.column("kind", sa.String()),
        sa.column("state", sa.String()),
        sa.column("incident_id", postgresql.UUID(as_uuid=True)),
        sa.column("opened_at", sa.DateTime(timezone=True)),
        sa.column("resolved_at", sa.DateTime(timezone=True)),
        sa.column("raise_event_id", postgresql.UUID(as_uuid=True)),
        sa.column("resolve_event_id", postgresql.UUID(as_uuid=True)),
    )
    offline_rows = bind.execute(
        sa.text(
            """
            SELECT device_id, offline_since
            FROM device_alert_state
            WHERE offline_notified = :notified
              AND offline_since IS NOT NULL
            """
        ),
        {"notified": True if _is_postgres() else 1},
    ).mappings().all()
    if offline_rows:
        op.bulk_insert(
            device_alerts,
            [
                {
                    "id": uuid.uuid4(),
                    "device_id": row["device_id"],
                    "kind": "offline",
                    "state": "open",
                    "incident_id": None,
                    "opened_at": row["offline_since"],
                    "resolved_at": None,
                    "raise_event_id": None,
                    "resolve_event_id": None,
                }
                for row in offline_rows
            ],
        )

    temp_rows = bind.execute(
        sa.text(
            """
            SELECT device_id, COALESCE(temp_last_alert_at, temp_last_sample_ts) AS opened_at
            FROM device_alert_state
            WHERE temp_level IN ('warning', 'critical')
              AND COALESCE(temp_last_alert_at, temp_last_sample_ts) IS NOT NULL
            """
        )
    ).mappings().all()
    if temp_rows:
        op.bulk_insert(
            device_alerts,
            [
                {
                    "id": uuid.uuid4(),
                    "device_id": row["device_id"],
                    "kind": "temperature",
                    "state": "open",
                    "incident_id": None,
                    "opened_at": row["opened_at"],
                    "resolved_at": None,
                    "raise_event_id": None,
                    "resolve_event_id": None,
                }
                for row in temp_rows
            ],
        )

    op.add_column("device_events", sa.Column("group_ids", _group_ids_type(), nullable=True))
    op.add_column(
        "device_events",
        sa.Column("group_snapshots", _json_type(), nullable=True),
    )

    if _is_postgres():
        op.execute(
            """
            UPDATE device_events
            SET group_ids = CASE
                    WHEN group_id IS NULL THEN ARRAY[]::uuid[]
                    ELSE ARRAY[group_id]
                END,
                group_snapshots = CASE
                    WHEN group_id IS NULL THEN '[]'::jsonb
                    ELSE jsonb_build_array(
                        jsonb_build_object(
                            'id', group_id::text,
                            'name', COALESCE(group_name, '')
                        )
                    )
                END
            """
        )
    else:
        op.execute(
            """
            UPDATE device_events
            SET group_ids = CASE
                    WHEN group_id IS NULL THEN '[]'
                    ELSE json_array(group_id)
                END,
                group_snapshots = CASE
                    WHEN group_id IS NULL THEN '[]'
                    ELSE json_array(json_object('id', group_id, 'name', COALESCE(group_name, '')))
                END
            """
        )

    op.alter_column("device_events", "group_ids", nullable=False)
    op.alter_column("device_events", "group_snapshots", nullable=False)
    if _is_postgres():
        op.create_index(
            "ix_device_events_group_ids_gin",
            "device_events",
            ["group_ids"],
            postgresql_using="gin",
        )

    _drop_fk("device_events", "device_id")
    op.create_foreign_key(
        "fk_device_events_device_id_devices",
        "device_events",
        "devices",
        ["device_id"],
        ["id"],
        ondelete="SET NULL",
    )

    _drop_fk("notifications", "group_id")
    op.create_foreign_key(
        "fk_notifications_group_id_device_groups",
        "notifications",
        "device_groups",
        ["group_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    raise NotImplementedError(
        "Downgrade of 0056 is intentionally not implemented per project policy."
    )
