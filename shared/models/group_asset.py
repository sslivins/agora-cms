"""GroupAsset junction model — tracks asset-to-group associations."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import DateTime, ForeignKey, UniqueConstraint
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.database import Base


class GroupAsset(Base):
    """Links an asset to a device group."""

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
    shared_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    asset: Mapped["Asset"] = relationship(back_populates="group_asset_links")
    # NOTE: GroupAsset.group relationship is added by cms/models/__init__.py
    # (DeviceGroup is a CMS-only model, not available in the worker package)
