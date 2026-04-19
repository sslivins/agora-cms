"""Pydantic schemas for device profile API."""

import re
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, field_validator


# Profile names become part of download filenames, so restrict to safe chars
_PROFILE_NAME_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}$")


class ProfileOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    description: str
    video_codec: str
    video_profile: str
    max_width: int
    max_height: int
    max_fps: int
    video_bitrate: str
    crf: int
    pixel_format: str
    color_space: str
    audio_codec: str
    audio_bitrate: str
    builtin: bool
    device_count: int = 0
    total_variants: int = 0
    ready_variants: int = 0
    matches_defaults: bool = False
    created_at: datetime


class ProfileCreate(BaseModel):
    name: str
    description: str = ""
    video_codec: str = "h264"
    video_profile: str = "main"
    max_width: int = 1920
    max_height: int = 1080
    max_fps: int = 30
    video_bitrate: str = ""
    crf: int = 23
    pixel_format: str = "auto"
    color_space: str = "auto"
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"

    @field_validator("name")
    @classmethod
    def validate_name(cls, v: str) -> str:
        if not _PROFILE_NAME_RE.match(v):
            raise ValueError(
                "Profile name must start with a letter or digit, "
                "contain only letters, digits, hyphens, and underscores, "
                "and be 1–64 characters long"
            )
        return v


class ProfileUpdate(BaseModel):
    description: Optional[str] = None
    video_codec: Optional[str] = None
    video_profile: Optional[str] = None
    max_width: Optional[int] = None
    max_height: Optional[int] = None
    max_fps: Optional[int] = None
    video_bitrate: Optional[str] = None
    crf: Optional[int] = None
    pixel_format: Optional[str] = None
    color_space: Optional[str] = None
    audio_codec: Optional[str] = None
    audio_bitrate: Optional[str] = None
