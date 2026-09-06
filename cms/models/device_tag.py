"""Group-scoped device tags.

A device tag is a label attached to devices *within one group*.  Tag identity
is ``(group_id, lower(name))``, so "Summer Promos" in Group A and "Summer
Promos" in Group B are two unrelated tags.  There is deliberately no global
tag namespace: a schedule can therefore never target "every device tagged X"
across group boundaries, which keeps the owning group the sole authorization
boundary (see ``Device.group_id``).

Tags carry no ACL of their own — read/write access is derived from the owning
group, so an operator with access to a group can create, rename, and delete
its tags without needing user-administration rights.

Contrast with ``cms.models.tag.Tag``, which is a *global* asset-library tag
with a global uniqueness index.  The two are unrelated entities.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import (
    DateTime,
    ForeignKey,
    Index,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base

# Default chip colour (Tailwind neutral-500), matching the asset tag default.
DEFAULT_DEVICE_TAG_COLOR = "#737373"


class DeviceTag(Base):
    """A label scoped to exactly one device group."""

    __tablename__ = "device_tags"
    __table_args__ = (
        # Case-insensitive uniqueness *within* a group, not across groups.
        Index(
            "uq_device_tags_group_name_lower",
            "group_id",
            text("lower(name)"),
            unique=True,
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_groups.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(64), nullable=False)
    color: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default=DEFAULT_DEVICE_TAG_COLOR,
        server_default=DEFAULT_DEVICE_TAG_COLOR,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    group: Mapped["DeviceGroup"] = relationship()

    @property
    def qualified_name(self) -> str:
        """``<Group>:<Tag>`` display form used by the scheduler target picker."""
        group_name = getattr(self.group, "name", None) or "?"
        return f"{group_name}:{self.name}"


class DeviceTagAssignment(Base):
    """Junction table: devices <-> device tags.

    The device's group and the tag's group must match; that invariant is
    enforced by the service layer (``cms.services.device_tags``) and re-checked
    whenever a device changes group — moving a device to a different group
    drops all of its tags rather than carrying them across.
    """

    __tablename__ = "device_tag_assignments"
    __table_args__ = (
        UniqueConstraint("device_id", "tag_id", name="uq_device_tag_assignment"),
        Index("idx_device_tag_assignments_tag_id", "tag_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("devices.id", ondelete="CASCADE"), nullable=False
    )
    tag_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_tags.id", ondelete="CASCADE"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        server_default=func.current_timestamp(),
    )

    tag: Mapped[DeviceTag] = relationship()
