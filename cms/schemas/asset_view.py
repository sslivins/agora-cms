"""Pydantic schemas for the AssetView (saved-views) API."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, List, Literal, Optional

from pydantic import BaseModel, Field, field_validator


# Whitelist of filter keys we accept. Keeping this explicit prevents the
# UI from accidentally persisting arbitrary garbage to the row and gives
# a typed contract for the listing endpoint to deserialize against.
ALLOWED_FILTER_KEYS = {
    "q",
    "type",
    "group_id",
    "uploader_id",
    "tag_id",
    "usage",
    "uploaded_after",
    "uploaded_before",
    "date_days",
    "order",
}

ALLOWED_VIEW_MODES = {"table", "grid"}


def _normalize_name(v: str) -> str:
    return v.strip()


class AssetViewFilters(BaseModel):
    """Filter snapshot embedded in a saved view.

    All fields optional -- a view may pin only a subset of the filter
    state (e.g. just ``type=video, usage=unused``).
    """

    q: Optional[str] = None
    type: Optional[str] = None
    group_id: Optional[str] = None
    uploader_id: Optional[str] = None
    tag_id: Optional[str] = None
    usage: Optional[Literal["used", "unused"]] = None
    uploaded_after: Optional[str] = None
    uploaded_before: Optional[str] = None
    date_days: Optional[str] = None
    order: Optional[str] = None
    view_mode: Optional[Literal["table", "grid"]] = None


class AssetViewIn(BaseModel):
    """Request body for ``POST /api/asset-views``."""

    name: str = Field(..., min_length=1, max_length=80)
    filters: AssetViewFilters = Field(default_factory=AssetViewFilters)
    is_default: bool = False

    @field_validator("name")
    @classmethod
    def _normalize(cls, v: str) -> str:
        v = _normalize_name(v)
        if not v:
            raise ValueError("name cannot be empty / whitespace-only")
        return v


class AssetViewPatch(BaseModel):
    """Request body for ``PATCH /api/asset-views/{id}``. All fields optional."""

    name: Optional[str] = Field(None, min_length=1, max_length=80)
    filters: Optional[AssetViewFilters] = None
    is_default: Optional[bool] = None

    @field_validator("name")
    @classmethod
    def _normalize(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = _normalize_name(v)
        if not v:
            raise ValueError("name cannot be empty / whitespace-only")
        return v


class AssetViewOut(BaseModel):
    """Single saved view as returned by the API."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    filters: dict[str, Any]
    is_default: bool
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
