"""Pydantic schemas for asset API."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from cms.models.asset import AssetType


class AssetOut(BaseModel):
    model_config = {"from_attributes": True}

    id: uuid.UUID
    filename: str
    asset_type: AssetType
    size_bytes: int
    checksum: str
    uploaded_at: datetime
