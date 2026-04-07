"""Device profile ORM model — defines hardware capabilities and transcoding targets."""

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from cms.database import Base


class DeviceProfile(Base):
    __tablename__ = "device_profiles"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, default="")

    # Video transcoding target
    video_codec: Mapped[str] = mapped_column(String(20), default="h264")  # h264, h265, etc.
    video_profile: Mapped[str] = mapped_column(String(20), default="main")  # baseline, main, high
    max_width: Mapped[int] = mapped_column(Integer, default=1920)
    max_height: Mapped[int] = mapped_column(Integer, default=1080)
    max_fps: Mapped[int] = mapped_column(Integer, default=30)
    video_bitrate: Mapped[str] = mapped_column(String(20), default="")  # e.g. "5M", empty = use CRF
    crf: Mapped[int] = mapped_column(Integer, default=23)
    pixel_format: Mapped[str] = mapped_column(String(20), default="auto")
    color_space: Mapped[str] = mapped_column(String(20), default="auto")
    audio_codec: Mapped[str] = mapped_column(String(20), default="aac")
    audio_bitrate: Mapped[str] = mapped_column(String(20), default="128k")

    # Whether this is a built-in (non-deletable) profile
    builtin: Mapped[bool] = mapped_column(Boolean, default=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(timezone.utc)
    )

    # Relationships
    devices: Mapped[list["Device"]] = relationship(back_populates="profile")
    variants: Mapped[list["AssetVariant"]] = relationship(back_populates="profile")
