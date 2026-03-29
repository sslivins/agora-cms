"""Asset ORM model, AssetVariant, and DeviceAsset tracking."""

import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


class AssetType(str, PyEnum):
    VIDEO = "video"
    IMAGE = "image"


class VariantStatus(str, PyEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    asset_type: Mapped[AssetType] = mapped_column(Enum(AssetType), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64), default="")  # SHA-256
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    schedules: Mapped[list["Schedule"]] = relationship(back_populates="asset")
    device_assets: Mapped[list["DeviceAsset"]] = relationship(back_populates="asset")
    variants: Mapped[list["AssetVariant"]] = relationship(back_populates="source_asset")


class AssetVariant(Base):
    """A transcoded version of a source asset for a specific device profile."""

    __tablename__ = "asset_variants"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id", ondelete="CASCADE"), nullable=False
    )
    profile_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_profiles.id", ondelete="CASCADE"), nullable=False
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)  # e.g. "video_pi-zero-2w.mp4"
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64), default="")

    status: Mapped[VariantStatus] = mapped_column(
        Enum(VariantStatus), default=VariantStatus.PENDING
    )
    progress: Mapped[float] = mapped_column(Float, default=0.0)  # 0.0 to 100.0
    error_message: Mapped[str] = mapped_column(Text, default="")

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    source_asset: Mapped[Asset] = relationship(back_populates="variants")
    profile: Mapped["DeviceProfile"] = relationship(back_populates="variants")


class DeviceAsset(Base):
    """Tracks which assets are currently on which device."""

    __tablename__ = "device_assets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    device_id: Mapped[str] = mapped_column(String(64), ForeignKey("devices.id"), nullable=False)
    asset_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id"), nullable=False
    )
    fetched_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    device: Mapped["Device"] = relationship(back_populates="device_assets")
    asset: Mapped[Asset] = relationship(back_populates="device_assets")
