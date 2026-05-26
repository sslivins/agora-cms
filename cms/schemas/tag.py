"""Pydantic schemas for the Tag API (Asset Library tagging)."""

from __future__ import annotations

import re
import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# Hex color (#RGB or #RRGGBB).  Anything outside this set is rejected --
# we render these into inline styles on the asset rows.
_HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _normalize_name(v: str) -> str:
    """Lower-trim a tag name to its canonical form."""
    return v.strip().lower()


class TagIn(BaseModel):
    """Request body for ``POST /api/tags``."""

    name: str = Field(..., min_length=1, max_length=64)
    color: Optional[str] = None  # defaults applied server-side

    @field_validator("name")
    @classmethod
    def _normalize(cls, v: str) -> str:
        v = _normalize_name(v)
        if not v:
            raise ValueError("name cannot be empty / whitespace-only")
        return v

    @field_validator("color")
    @classmethod
    def _validate_color(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _HEX_COLOR_RE.match(v):
            raise ValueError("color must be a hex string like '#aabbcc' or '#abc'")
        return v.lower()


class TagPatch(BaseModel):
    """Request body for ``PATCH /api/tags/{id}``.  All fields optional."""

    name: Optional[str] = Field(None, min_length=1, max_length=64)
    color: Optional[str] = None

    @field_validator("name")
    @classmethod
    def _normalize(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        v = _normalize_name(v)
        if not v:
            raise ValueError("name cannot be empty / whitespace-only")
        return v

    @field_validator("color")
    @classmethod
    def _validate_color(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        if not _HEX_COLOR_RE.match(v):
            raise ValueError("color must be a hex string like '#aabbcc' or '#abc'")
        return v.lower()


class TagOut(BaseModel):
    """Single tag record, used both standalone and embedded in ``AssetOut``."""

    model_config = {"from_attributes": True}

    id: uuid.UUID
    name: str
    color: str
    created_at: Optional[datetime] = None
    asset_count: Optional[int] = None  # only populated by ``GET /api/tags``
