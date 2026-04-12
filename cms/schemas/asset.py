"""Pydantic schemas for asset API."""

import uuid
from datetime import datetime
from typing import Optional

from pydantic import BaseModel

from cms.models.asset import AssetType, VariantStatus


class AssetVariantOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    profile_id: uuid.UUID
    profile_name: str = ""
    filename: str
    size_bytes: int
    checksum: str
    status: VariantStatus
    progress: float
    error_message: str = ""
    created_at: datetime
    completed_at: Optional[datetime] = None


class AssetOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    filename: str
    original_filename: Optional[str] = None
    asset_type: AssetType
    size_bytes: int
    checksum: str
    duration_seconds: Optional[float] = None
    uploaded_at: datetime
    owner_group_id: Optional[uuid.UUID] = None
    is_global: bool = False
    uploaded_by_user_id: Optional[uuid.UUID] = None
