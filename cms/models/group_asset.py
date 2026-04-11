"""GroupAsset junction model — tracks asset ownership and cross-group sharing."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


class GroupAsset(Base):
    """Links an asset to a device group.

    is_owner=True means this group uploaded the asset.
    is_owner=False means the asset was shared with this group.
    """

    __tablename__ = "group_assets"
    __table_args__ = (
        UniqueConstraint("asset_id", "group_id", name="uq_group_asset"),
    )

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    group_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_groups.id", ondelete="CASCADE"), nullable=False
    )
    is_owner: Mapped[bool] = mapped_column(Boolean, default=False)
    shared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    asset: Mapped["Asset"] = relationship()
    group: Mapped["DeviceGroup"] = relationship()
