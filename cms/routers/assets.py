"""Asset library API routes."""

import hashlib
import re
import uuid
from typing import List

from fastapi import APIRouter, Depends, HTTPException, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from cms.auth import get_settings, require_auth
from cms.config import Settings
from cms.database import get_db
from cms.models.asset import Asset, AssetType
from cms.schemas.asset import AssetOut

router = APIRouter(prefix="/api/assets", dependencies=[Depends(require_auth)])

ALLOWED_PATTERN = re.compile(r"^[a-zA-Z0-9_\-][a-zA-Z0-9_\-. ]{0,200}\.(mp4|jpg|jpeg|png)$")
MAX_UPLOAD_BYTES = 500 * 1024 * 1024


def _asset_type(filename: str) -> AssetType:
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "mp4":
        return AssetType.VIDEO
    return AssetType.IMAGE


@router.get("", response_model=List[AssetOut])
async def list_assets(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Asset).order_by(Asset.uploaded_at.desc()))
    return result.scalars().all()


@router.post("/upload", response_model=AssetOut, status_code=201)
async def upload_asset(
    file: UploadFile,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    if not file.filename or not ALLOWED_PATTERN.match(file.filename):
        raise HTTPException(status_code=400, detail="Invalid filename")

    # Check for duplicate
    existing = await db.execute(select(Asset).where(Asset.filename == file.filename))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Asset already exists")

    # Read and hash
    content = await file.read()
    if len(content) > MAX_UPLOAD_BYTES:
        raise HTTPException(status_code=413, detail="File too large")
    checksum = hashlib.sha256(content).hexdigest()

    # Store file
    storage_dir = settings.asset_storage_path
    storage_dir.mkdir(parents=True, exist_ok=True)
    dest = storage_dir / file.filename
    dest.write_bytes(content)

    # Database record
    asset = Asset(
        filename=file.filename,
        asset_type=_asset_type(file.filename),
        size_bytes=len(content),
        checksum=checksum,
    )
    db.add(asset)
    await db.commit()
    await db.refresh(asset)
    return asset


@router.get("/{asset_id}", response_model=AssetOut)
async def get_asset(asset_id: uuid.UUID, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")
    return asset


@router.delete("/{asset_id}")
async def delete_asset(
    asset_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    settings: Settings = Depends(get_settings),
):
    result = await db.execute(select(Asset).where(Asset.id == asset_id))
    asset = result.scalar_one_or_none()
    if not asset:
        raise HTTPException(status_code=404, detail="Asset not found")

    # Remove file
    file_path = settings.asset_storage_path / asset.filename
    if file_path.is_file():
        file_path.unlink()

    await db.delete(asset)
    await db.commit()
    return {"deleted": asset.filename}
