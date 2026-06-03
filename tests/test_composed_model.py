"""Phase 0 tests for the ComposedSlide ORM model."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from cms.composed.schema import empty_layout
from cms.models.asset import Asset, AssetType
from cms.models.composed_slide import ComposedSlide


@pytest.mark.asyncio
async def test_composed_slide_can_be_persisted_and_queried(db_session):
    asset = Asset(
        filename="composed-1",
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
    )
    db_session.add(asset)
    await db_session.commit()

    cs = ComposedSlide(
        asset_id=asset.id,
        layout_json=empty_layout().model_dump(mode="json"),
    )
    db_session.add(cs)
    await db_session.commit()

    res = await db_session.execute(
        select(ComposedSlide).where(ComposedSlide.asset_id == asset.id)
    )
    fetched = res.scalar_one()
    assert fetched.asset_id == asset.id
    assert fetched.is_draft is True
    assert fetched.schema_version == 1
    assert fetched.bundle_built_at is None
    assert fetched.last_ai_prompt is None
    assert fetched.layout_json["schema_version"] == 1


@pytest.mark.asyncio
async def test_composed_slide_cascade_deletes_with_asset(db_session):
    asset = Asset(
        filename="composed-2",
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
    )
    db_session.add(asset)
    await db_session.commit()

    cs = ComposedSlide(
        asset_id=asset.id,
        layout_json=empty_layout().model_dump(mode="json"),
    )
    db_session.add(cs)
    await db_session.commit()
    cs_id = cs.id

    await db_session.delete(asset)
    await db_session.commit()

    res = await db_session.execute(
        select(ComposedSlide).where(ComposedSlide.id == cs_id)
    )
    assert res.scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_composed_slide_asset_id_is_unique(db_session):
    from sqlalchemy.exc import IntegrityError

    asset = Asset(
        filename="composed-3",
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
    )
    db_session.add(asset)
    await db_session.commit()

    db_session.add(
        ComposedSlide(
            asset_id=asset.id,
            layout_json=empty_layout().model_dump(mode="json"),
        )
    )
    await db_session.commit()

    db_session.add(
        ComposedSlide(
            asset_id=asset.id,
            layout_json=empty_layout().model_dump(mode="json"),
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.commit()
    await db_session.rollback()


def test_asset_type_includes_composed():
    assert AssetType.COMPOSED.value == "composed"
