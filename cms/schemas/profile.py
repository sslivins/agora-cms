"""Pydantic schemas for device profile API."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel


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
    audio_codec: str
    audio_bitrate: str
    builtin: bool
    device_count: int = 0
    total_variants: int = 0
    ready_variants: int = 0
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
    audio_codec: str = "aac"
    audio_bitrate: str = "128k"


class ProfileUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    video_profile: Optional[str] = None
    max_width: Optional[int] = None
    max_height: Optional[int] = None
    max_fps: Optional[int] = None
    video_bitrate: Optional[str] = None
    crf: Optional[int] = None
    audio_codec: Optional[str] = None
    audio_bitrate: Optional[str] = None
