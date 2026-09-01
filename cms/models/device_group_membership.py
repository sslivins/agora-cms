"""Device ↔ group membership join table (device groups many-to-many, #863).

Mirrors :class:`cms.models.user.UserGroup`: a composite-PK association table
with ``ON DELETE CASCADE`` on both foreign keys. This is the *expand* half of
the expand/contract migration away from the single ``devices.group_id`` FK.

During the blue/green coexistence window this table is kept as an exact mirror
of ``devices.group_id`` (dual-write, at most one membership per device — see
:func:`cms.services.device_membership.set_single_group_membership`). True
multi-membership is only enabled in a later stage, once every replica reads
memberships instead of ``group_id``.
"""

import uuid

from sqlalchemy import ForeignKey, Index, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


class DeviceGroupMembership(Base):
    """Junction row: a device belongs to a device group."""

    __tablename__ = "device_group_memberships"
    __table_args__ = (
        # Reverse-direction lookup ("which devices are in this group") — the
        # PK already covers (device_id, group_id) for the forward direction.
        Index("ix_device_group_memberships_group_device", "group_id", "device_id"),
    )

    device_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("devices.id", ondelete="CASCADE"), primary_key=True
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("device_groups.id", ondelete="CASCADE"),
        primary_key=True,
    )

    device: Mapped["Device"] = relationship(back_populates="memberships")  # noqa: F821
    group: Mapped["DeviceGroup"] = relationship()  # noqa: F821
