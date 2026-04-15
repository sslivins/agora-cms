"""Device and DeviceGroup ORM models."""

import uuid
from datetime import datetime, timezone
from enum import Enum as PyEnum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


class DeviceStatus(str, PyEnum):
    PENDING = "pending"
    ADOPTED = "adopted"
    ORPHANED = "orphaned"


class DeviceGroup(Base):
    __tablename__ = "device_groups"
    __table_args__ = {"extend_existing": True}

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")
    default_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    devices: Mapped[list["Device"]] = relationship(back_populates="group")
    default_asset: Mapped["Asset | None"] = relationship(foreign_keys=[default_asset_id])


class Device(Base):
    __tablename__ = "devices"
    __table_args__ = {"extend_existing": True}

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # Pi serial or UUID
    name: Mapped[str] = mapped_column(String(100), default="")
    location: Mapped[str] = mapped_column(String(255), default="")
    status: Mapped[DeviceStatus] = mapped_column(
        Enum(DeviceStatus), default=DeviceStatus.PENDING
    )
    group_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_groups.id"), nullable=True
    )
    firmware_version: Mapped[str] = mapped_column(String(32), default="")
    storage_capacity_mb: Mapped[int] = mapped_column(Integer, default=0)
    storage_used_mb: Mapped[int] = mapped_column(Integer, default=0)
    device_type: Mapped[str] = mapped_column(String(100), default="")
    supported_codecs: Mapped[str] = mapped_column(String(100), default="")
    profile_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("device_profiles.id"), nullable=True
    )
    default_asset_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("assets.id"), nullable=True
    )
    timezone: Mapped[str | None] = mapped_column(String(64), nullable=True)
    device_auth_token_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    device_api_key_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    previous_api_key_hash: Mapped[str | None] = mapped_column(String(128), nullable=True)
    api_key_rotated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_seen: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    registered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    group: Mapped[DeviceGroup | None] = relationship(back_populates="devices")
    profile: Mapped["DeviceProfile | None"] = relationship(back_populates="devices")
    default_asset: Mapped["Asset | None"] = relationship(foreign_keys="[Device.default_asset_id]")
    device_assets: Mapped[list["DeviceAsset"]] = relationship(back_populates="device")
