"""Phase 1A tests for the composed slide publish service."""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from sqlalchemy import select

from cms.composed.publish import (
    PublishError,
    publish_composed_slide,
)
from cms.composed.schema import (
    Cell,
    Layout,
    WidgetInstance,
    empty_layout,
)
from cms.models.asset import Asset, AssetType
from cms.models.composed_slide import ComposedSlide


def _text_layout(text: str = "hello world") -> Layout:
    layout = empty_layout()
    layout.widgets.append(
        WidgetInstance(
            id=uuid.uuid4(),
            type="text",
            cell=Cell(row=1, col=1, rowspan=1, colspan=4),
            config={"text": text, "font_size_px": 64},
            config_version=1,
        )
    )
    return layout


async def _make_composed(db_session, *, layout: Layout | None = None) -> tuple[Asset, ComposedSlide]:
    asset = Asset(
        filename=f"composed-placeholder-{uuid.uuid4()}",
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
    )
    db_session.add(asset)
    await db_session.flush()

    cs = ComposedSlide(
        asset_id=asset.id,
        layout_json=(layout or _text_layout()).model_dump(mode="json"),
    )
    db_session.add(cs)
    await db_session.flush()
    return asset, cs


@pytest.fixture
def mock_storage_and_dir(tmp_path):
    """Patch get_storage + asset_storage_path to tmp_path."""
    mock_storage = MagicMock()
    mock_storage.on_file_stored = AsyncMock()

    mock_settings = MagicMock()
    mock_settings.asset_storage_path = tmp_path

    with patch("cms.composed.publish.get_storage", return_value=mock_storage), \
         patch("cms.auth.get_settings", return_value=mock_settings):
        yield mock_storage, tmp_path


@pytest.mark.asyncio
async def test_publish_writes_bundle_and_updates_asset(db_session, mock_storage_and_dir):
    mock_storage, tmp_path = mock_storage_and_dir
    asset, _ = await _make_composed(db_session)
    asset_id = asset.id

    result = await publish_composed_slide(asset_id, db_session)

    assert result.rebuilt is True
    assert result.size_bytes > 0
    assert len(result.checksum) == 64  # SHA-256 hex

    # Asset row was updated in-place
    await db_session.refresh(asset)
    assert asset.filename == result.filename
    assert asset.checksum == result.checksum
    assert asset.size_bytes == result.size_bytes
    assert asset.asset_type == AssetType.COMPOSED

    # Bundle landed on disk
    bundle_path = tmp_path / asset.filename
    assert bundle_path.is_file()
    assert bundle_path.read_bytes() == bundle_path.read_bytes()  # tautology — exists

    # Cloud sync hook fired
    mock_storage.on_file_stored.assert_awaited_once_with(asset.filename)


@pytest.mark.asyncio
async def test_publish_flips_draft_and_records_metadata(db_session, mock_storage_and_dir):
    asset, cs = await _make_composed(db_session)
    assert cs.is_draft is True

    await publish_composed_slide(asset.id, db_session)

    await db_session.refresh(cs)
    assert cs.is_draft is False
    assert cs.bundle_built_at is not None
    # Empty source_asset_ids on a text-only layout is fine (no refs)
    assert cs.bundle_source_asset_ids == []


@pytest.mark.asyncio
async def test_publish_filename_is_content_addressed(db_session, mock_storage_and_dir):
    asset, _ = await _make_composed(db_session)
    asset_id = asset.id
    result = await publish_composed_slide(asset_id, db_session)
    # composed-{uuid}-{12 hex chars}.html
    assert result.filename.startswith(f"composed-{asset_id}-")
    assert result.filename.endswith(".html")
    sha_chunk = result.filename[len(f"composed-{asset_id}-"):-len(".html")]
    assert len(sha_chunk) == 12
    assert all(c in "0123456789abcdef" for c in sha_chunk)


@pytest.mark.asyncio
async def test_publish_idempotent_when_content_unchanged(db_session, mock_storage_and_dir):
    mock_storage, _ = mock_storage_and_dir
    asset, cs = await _make_composed(db_session)
    asset_id = asset.id

    first = await publish_composed_slide(asset_id, db_session)
    assert first.rebuilt is True
    assert mock_storage.on_file_stored.await_count == 1

    second = await publish_composed_slide(asset_id, db_session)
    assert second.rebuilt is False
    assert second.checksum == first.checksum
    assert second.filename == first.filename
    # Second call did NOT re-upload — still only one storage hook call
    assert mock_storage.on_file_stored.await_count == 1
    # bundle_built_at still bumped on no-op publish
    assert second.bundle_built_at >= first.bundle_built_at


@pytest.mark.asyncio
async def test_publish_rebuilds_when_layout_changes(db_session, mock_storage_and_dir):
    mock_storage, _ = mock_storage_and_dir
    asset, cs = await _make_composed(db_session, layout=_text_layout("first"))
    asset_id = asset.id

    first = await publish_composed_slide(asset_id, db_session)

    cs.layout_json = _text_layout("second").model_dump(mode="json")
    await db_session.flush()

    second = await publish_composed_slide(asset_id, db_session)

    assert second.rebuilt is True
    assert second.checksum != first.checksum
    assert second.filename != first.filename
    assert mock_storage.on_file_stored.await_count == 2


@pytest.mark.asyncio
async def test_publish_raises_for_missing_asset(db_session, mock_storage_and_dir):
    with pytest.raises(PublishError, match="not found"):
        await publish_composed_slide(uuid.uuid4(), db_session)


@pytest.mark.asyncio
async def test_publish_raises_for_non_composed_asset(db_session, mock_storage_and_dir):
    asset = Asset(
        filename="some-image.jpg",
        asset_type=AssetType.IMAGE,
        size_bytes=0,
        checksum="",
    )
    db_session.add(asset)
    await db_session.flush()

    with pytest.raises(PublishError, match="not composed"):
        await publish_composed_slide(asset.id, db_session)


@pytest.mark.asyncio
async def test_publish_raises_when_no_composed_row(db_session, mock_storage_and_dir):
    asset = Asset(
        filename="orphan",
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
    )
    db_session.add(asset)
    await db_session.flush()

    with pytest.raises(PublishError, match="No composed slide row"):
        await publish_composed_slide(asset.id, db_session)


@pytest.mark.asyncio
async def test_publish_raises_on_invalid_layout(db_session, mock_storage_and_dir):
    asset = Asset(
        filename="bad",
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
    )
    db_session.add(asset)
    await db_session.flush()
    cs = ComposedSlide(
        asset_id=asset.id,
        layout_json={"schema_version": "not-an-int", "widgets": "not-a-list"},
    )
    db_session.add(cs)
    await db_session.flush()

    with pytest.raises(PublishError, match="Invalid layout JSON"):
        await publish_composed_slide(asset.id, db_session)


@pytest.mark.asyncio
async def test_publish_raises_on_validation_failure(db_session, mock_storage_and_dir):
    # Two overlapping widgets — passes Pydantic shape, fails validate_layout.
    layout = empty_layout()
    layout.widgets.append(
        WidgetInstance(
            id=uuid.uuid4(),
            type="text",
            cell=Cell(row=1, col=1, rowspan=2, colspan=4),
            config={"text": "a"},
            config_version=1,
        )
    )
    layout.widgets.append(
        WidgetInstance(
            id=uuid.uuid4(),
            type="text",
            cell=Cell(row=2, col=2, rowspan=2, colspan=2),
            config={"text": "b"},
            config_version=1,
        )
    )
    asset, _ = await _make_composed(db_session, layout=layout)

    with pytest.raises(PublishError, match="failed validation"):
        await publish_composed_slide(asset.id, db_session)
