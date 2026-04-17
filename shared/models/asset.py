"""Asset ORM model, AssetVariant, and DeviceAsset tracking."""

import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import Boolean, DateTime, Enum, Float, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from shared.database import Base


class AssetType(str, PyEnum):
    VIDEO = "video"
    IMAGE = "image"
    WEBPAGE = "webpage"
    STREAM = "stream"          # live stream — played directly via URL
    SAVED_STREAM = "saved_stream"  # captured stream — downloaded & transcoded for offline playback


class VariantStatus(str, PyEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    READY = "ready"
    FAILED = "failed"


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    filename: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    original_filename: Mapped[str | None] = mapped_column(String(255), nullable=True)  # set when converted (e.g. HEIC→JPG)
    display_name: Mapped[str | None] = mapped_column(String(255), nullable=True)  # user-editable friendly name
    asset_type: Mapped[AssetType] = mapped_column(Enum(AssetType), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64), default="")  # SHA-256
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # When True, asset is visible to all groups regardless of group associations
    is_global: Mapped[bool] = mapped_column(
        "is_global", nullable=False, default=False, server_default="false"
    )
    # Who uploaded this asset (for personal/no-group assets visibility)
    uploaded_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    # Media metadata (populated via ffprobe after upload)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)  # bps
    frame_rate: Mapped[str | None] = mapped_column(String(16), nullable=True)  # e.g. "30" or "29.97"
    color_space: Mapped[str | None] = mapped_column(String(64), nullable=True)  # e.g. "bt709", "bt2020"

    # URL for webpage/stream assets (populated when asset_type is WEBPAGE, STREAM, or SAVED_STREAM)
    url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    # Max capture duration in seconds (for SAVED_STREAM of live sources)
    capture_duration: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Soft-delete marker.  Set to now() when the user hits DELETE.  The API
    # treats rows with deleted_at IS NOT NULL as gone; a background reaper
    # loop in the CMS (deleted_asset_reaper_loop) cleans up blobs + hard-
    # deletes the row once all associated Jobs reach a terminal state.
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )

    # NOTE: Asset.schedules relationship is added by cms/models/__init__.py
    # (Schedule is a CMS-only model, not available in the worker package)
    device_assets: Mapped[list["DeviceAsset"]] = relationship(back_populates="asset")
    variants: Mapped[list["AssetVariant"]] = relationship(back_populates="source_asset")
    group_asset_links: Mapped[list["GroupAsset"]] = relationship(back_populates="asset")


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
    filename: Mapped[str] = mapped_column(String(255), nullable=False)  # "{uuid}.mp4" — UUID-based, no profile/asset name
    size_bytes: Mapped[int] = mapped_column(Integer, default=0)
    checksum: Mapped[str] = mapped_column(String(64), default="")

    # Media metadata (populated via ffprobe after transcoding)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    duration_seconds: Mapped[float | None] = mapped_column(Float, nullable=True)
    video_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    audio_codec: Mapped[str | None] = mapped_column(String(64), nullable=True)
    bitrate: Mapped[int | None] = mapped_column(Integer, nullable=True)
    frame_rate: Mapped[str | None] = mapped_column(String(16), nullable=True)
    color_space: Mapped[str | None] = mapped_column(String(64), nullable=True)

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

    # NOTE: DeviceAsset.device relationship is added by cms/models/__init__.py
    # (Device is a CMS-only model, not available in the worker package)
    asset: Mapped[Asset] = relationship(back_populates="device_assets")
