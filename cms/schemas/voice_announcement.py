"""API schemas for voice-announcement builder endpoints."""

from __future__ import annotations

import uuid
from datetime import datetime

from pydantic import BaseModel, Field, field_validator

from shared.models.job import JobStatus


MAX_VOICE_ANNOUNCEMENT_SCRIPT_CHARS = 2000


def _strip_or_none(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


class VoiceOptionOut(BaseModel):
    short_name: str
    display_name: str
    locale: str
    emotions: list[str] = Field(default_factory=list)


class VoiceCatalogOut(BaseModel):
    voices: list[VoiceOptionOut] = Field(default_factory=list)
    available: bool = True
    message: str | None = None


class VoiceAnnouncementSynthesisBase(BaseModel):
    script_text: str = Field(..., min_length=1, max_length=MAX_VOICE_ANNOUNCEMENT_SCRIPT_CHARS)
    voice_name: str = Field(..., min_length=1, max_length=128)
    emotion: str | None = Field(default=None, max_length=64)
    language: str = Field(default="en-US", min_length=2, max_length=16)
    speech_rate: str | None = Field(default=None, max_length=16)

    @field_validator("script_text", "voice_name", "language")
    @classmethod
    def _strip_required(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped

    @field_validator("emotion", "speech_rate")
    @classmethod
    def _strip_optional(cls, value: str | None) -> str | None:
        return _strip_or_none(value)


class VoiceAnnouncementCreate(VoiceAnnouncementSynthesisBase):
    display_name: str = Field(..., min_length=1, max_length=255)

    @field_validator("display_name")
    @classmethod
    def _strip_display_name(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("must not be empty")
        return stripped


class VoiceAnnouncementUpdate(VoiceAnnouncementSynthesisBase):
    display_name: str | None = Field(default=None, min_length=1, max_length=255)

    @field_validator("display_name")
    @classmethod
    def _strip_optional_display_name(cls, value: str | None) -> str | None:
        return _strip_or_none(value)


class VoiceAnnouncementPreviewIn(VoiceAnnouncementSynthesisBase):
    pass


class VoiceAnnouncementStatusOut(BaseModel):
    asset_id: uuid.UUID
    generation_status: JobStatus
    generation_error: str | None = None
    last_generated_at: datetime | None = None


class VoiceAnnouncementCreateOut(BaseModel):
    asset_id: uuid.UUID
    generation_status: JobStatus
    edit_url: str
