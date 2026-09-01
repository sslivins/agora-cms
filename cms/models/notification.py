"""Notification ORM models."""

import uuid
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, event
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, Session, mapped_column, relationship
from sqlalchemy.types import JSON

from cms.database import Base

_JSON = JSON().with_variant(JSONB(), "postgresql")


class NotificationGroup(Base):
    """Group-target row for a shared notification."""

    __tablename__ = "notification_groups"
    __table_args__ = (
        Index(
            "ix_notification_groups_group_notification",
            "group_id",
            "notification_id",
        ),
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        primary_key=True,
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )

    notification: Mapped["Notification"] = relationship(back_populates="group_targets")
    group: Mapped["DeviceGroup"] = relationship()


class NotificationRead(Base):
    """Per-user read/dismiss state for a notification."""

    __tablename__ = "notification_reads"
    __table_args__ = (
        Index(
            "ix_notification_reads_user_dismissed",
            "user_id",
            "dismissed_at",
        ),
    )

    notification_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("notifications.id", ondelete="CASCADE"),
        primary_key=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    dismissed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    notification: Mapped["Notification"] = relationship(back_populates="reads")
    user: Mapped["User"] = relationship()


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    scope: Mapped[str] = mapped_column(
        String(20), nullable=False, default="system",
        doc="Visibility scope: 'system', 'group', or 'user'",
    )
    level: Mapped[str] = mapped_column(
        String(20), nullable=False, default="info",
        doc="Severity: 'info', 'success', 'warning', or 'error'",
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    message: Mapped[str] = mapped_column(Text, default="")
    details: Mapped[dict | None] = mapped_column(_JSON, nullable=True)

    # Legacy scalar targets kept for the Stage 5 compatibility window.
    # Group notifications now target one-or-more groups via
    # ``notification_groups``; ``group_id`` is the primary / historical
    # pointer only and is no longer the source of truth for visibility.
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_groups.id", ondelete="SET NULL"),
        nullable=True,
    )
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=True,
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        index=True,
    )
    read_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    group: Mapped["DeviceGroup | None"] = relationship()
    user: Mapped["User | None"] = relationship()
    group_targets: Mapped[list[NotificationGroup]] = relationship(
        back_populates="notification",
        cascade="all, delete-orphan",
    )
    reads: Mapped[list[NotificationRead]] = relationship(
        back_populates="notification",
        cascade="all, delete-orphan",
    )

    def set_group_targets(self, group_ids: Iterable[uuid.UUID]) -> None:
        """Replace the notification's group target set."""
        deduped: list[uuid.UUID] = []
        seen: set[uuid.UUID] = set()
        for raw in group_ids:
            gid = raw if isinstance(raw, uuid.UUID) else uuid.UUID(str(raw))
            if gid in seen:
                continue
            seen.add(gid)
            deduped.append(gid)
        self.group_targets = [NotificationGroup(group_id=gid) for gid in deduped]
        self.group_id = deduped[0] if deduped else None

    @property
    def target_group_ids(self) -> list[uuid.UUID]:
        """Return the full effective target group set."""
        loaded_targets = self.__dict__.get("group_targets")
        if loaded_targets:
            return [row.group_id for row in loaded_targets]
        return [self.group_id] if self.group_id is not None else []


@event.listens_for(Session, "before_flush")
def _sync_notification_group_targets(session, flush_context, instances) -> None:
    """Mirror legacy ``group_id`` writes into ``notification_groups``.

    Direct ``Notification(...)`` construction is common in the codebase and
    tests. Keep that API working during the migration by synthesizing the join
    rows automatically whenever a notification is flushed.
    """
    for obj in session.new.union(session.dirty):
        if not isinstance(obj, Notification):
            continue

        if obj.scope != "group":
            if obj.group_targets:
                obj.group_targets = []
            obj.group_id = None
            continue

        if obj.group_targets:
            deduped: list[NotificationGroup] = []
            seen: set[uuid.UUID] = set()
            for row in obj.group_targets:
                if row.group_id in seen:
                    continue
                seen.add(row.group_id)
                deduped.append(row)
            if len(deduped) != len(obj.group_targets):
                obj.group_targets = deduped
            obj.group_id = deduped[0].group_id if deduped else obj.group_id
            continue

        if obj.group_id is not None:
            obj.group_targets = [NotificationGroup(group_id=obj.group_id)]
