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
from cms.models.slideshow_slide import SlideshowSlide


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
    # Out-of-bounds widget — passes Pydantic shape, fails validate_layout.
    # (Overlap is allowed now, so it can no longer be the failure trigger.)
    layout = empty_layout()
    layout.widgets.append(
        WidgetInstance(
            id=uuid.uuid4(),
            type="text",
            cell=Cell(row=2, col=1, rowspan=8, colspan=1),
            config={"text": "a"},
            config_version=1,
        )
    )
    asset, _ = await _make_composed(db_session, layout=layout)

    with pytest.raises(PublishError, match="failed validation"):
        await publish_composed_slide(asset.id, db_session)


# ---------------------------------------------------------------------------
# MediaWidget bucketing tests (Phase 1C)
# ---------------------------------------------------------------------------
#
# These exercise the publish-layer split: each declared asset is routed
# to one of three fates based on its AssetType.  IMAGE → inlined as a
# data URI (bytes channel).  VIDEO → emitted as a sibling URL the device
# resolves against its local /assets cache.  Anything else → PublishError.


def _media_layout(asset_id: uuid.UUID, *, cell: Cell | None = None) -> Layout:
    layout = empty_layout()
    layout.widgets.append(
        WidgetInstance(
            id=uuid.uuid4(),
            type="media",
            cell=cell or Cell(row=1, col=1, rowspan=2, colspan=4),
            config={"asset_id": str(asset_id), "object_fit": "cover", "alt": ""},
            config_version=1,
        )
    )
    return layout


async def _make_image_asset(db_session, tmp_path, filename: str = "pic.png") -> Asset:
    # MediaWidget's image branch inlines the file bytes — the publish
    # layer reads from disk via asset_storage_path, so the file must
    # actually exist at tmp_path / filename.
    img = Asset(
        filename=filename,
        asset_type=AssetType.IMAGE,
        size_bytes=8,
        checksum="x",
    )
    db_session.add(img)
    await db_session.flush()
    (tmp_path / filename).write_bytes(b"\x89PNG\r\n\x1a\n")
    return img


async def _make_video_asset(db_session, filename: str = "clip.mp4") -> Asset:
    # Videos go through the sibling-URL channel — publish doesn't touch
    # the file bytes, so we don't need to write anything to disk.
    vid = Asset(
        filename=filename,
        asset_type=AssetType.VIDEO,
        size_bytes=1024,
        checksum="y",
    )
    db_session.add(vid)
    await db_session.flush()
    return vid


@pytest.mark.asyncio
async def test_publish_image_via_media_widget_inlines_bytes(db_session, mock_storage_and_dir):
    _, tmp_path = mock_storage_and_dir
    img = await _make_image_asset(db_session, tmp_path)
    asset, cs = await _make_composed(db_session, layout=_media_layout(img.id))

    result = await publish_composed_slide(asset.id, db_session)

    bundle_html = (tmp_path / asset.filename).read_text()
    # IMAGE went through the bytes channel — emitted as a data URI
    assert "data:image/png;base64," in bundle_html
    assert "/assets/videos/" not in bundle_html

    await db_session.refresh(cs)
    assert str(img.id) in [str(x) for x in cs.bundle_source_asset_ids]
    assert result.rebuilt is True


@pytest.mark.asyncio
async def test_publish_video_via_media_widget_emits_sibling_url(db_session, mock_storage_and_dir):
    _, tmp_path = mock_storage_and_dir
    vid = await _make_video_asset(db_session, "clip.mp4")
    asset, cs = await _make_composed(db_session, layout=_media_layout(vid.id))

    await publish_composed_slide(asset.id, db_session)

    bundle_html = (tmp_path / asset.filename).read_text()
    # VIDEO went through the sibling URL channel — no inlined bytes
    assert "<video " in bundle_html
    assert "/assets/videos/clip.mp4" in bundle_html
    assert "data:video" not in bundle_html

    await db_session.refresh(cs)
    assert str(vid.id) in [str(x) for x in cs.bundle_source_asset_ids]


@pytest.mark.asyncio
async def test_publish_video_filename_is_url_encoded(db_session, mock_storage_and_dir):
    _, tmp_path = mock_storage_and_dir
    # Spaces, parens, ampersand — all need URL-encoding to form a valid
    # src attribute.  The HTML attribute value still needs to round-trip
    # safely (no double-escape that breaks the path on-device).
    vid = await _make_video_asset(db_session, "my video (final) & cut.mp4")
    asset, _ = await _make_composed(db_session, layout=_media_layout(vid.id))

    await publish_composed_slide(asset.id, db_session)

    bundle_html = (tmp_path / asset.filename).read_text()
    # urllib.parse.quote default-safe set leaves "/" alone; everything
    # else interesting gets percent-encoded.
    assert "my%20video%20%28final%29%20%26%20cut.mp4" in bundle_html
    # Sanity: the raw form did NOT leak through
    assert "my video (final)" not in bundle_html


@pytest.mark.asyncio
async def test_publish_rejects_non_media_asset_type(db_session, mock_storage_and_dir):
    # A webpage asset has no business being targeted by MediaWidget —
    # the publish layer is the second line of defense (the first being
    # the UI filter on the editor).
    web = Asset(
        filename="page.url",
        asset_type=AssetType.WEBPAGE,
        size_bytes=0,
        checksum="",
    )
    db_session.add(web)
    await db_session.flush()
    asset, _ = await _make_composed(db_session, layout=_media_layout(web.id))

    with pytest.raises(PublishError, match="only IMAGE and VIDEO"):
        await publish_composed_slide(asset.id, db_session)


@pytest.mark.asyncio
async def test_publish_rejects_missing_asset_reference(db_session, mock_storage_and_dir):
    # User deleted the asset between save and publish — publish must
    # surface this as a clean PublishError, not a 500.
    ghost = uuid.uuid4()
    asset, _ = await _make_composed(db_session, layout=_media_layout(ghost))

    with pytest.raises(PublishError, match="missing asset"):
        await publish_composed_slide(asset.id, db_session)


@pytest.mark.asyncio
async def test_publish_warns_when_multiple_video_widgets_declared(
    db_session, mock_storage_and_dir, caplog
):
    vid1 = await _make_video_asset(db_session, "a.mp4")
    vid2 = await _make_video_asset(db_session, "b.mp4")
    layout = empty_layout()
    layout.widgets.extend([
        WidgetInstance(
            id=uuid.uuid4(),
            type="media",
            cell=Cell(row=1, col=1, rowspan=2, colspan=4),
            config={"asset_id": str(vid1.id), "object_fit": "cover", "alt": ""},
            config_version=1,
        ),
        WidgetInstance(
            id=uuid.uuid4(),
            type="media",
            cell=Cell(row=3, col=1, rowspan=2, colspan=4),
            config={"asset_id": str(vid2.id), "object_fit": "cover", "alt": ""},
            config_version=1,
        ),
    ])
    asset, _ = await _make_composed(db_session, layout=layout)

    import logging
    with caplog.at_level(logging.WARNING, logger="cms.composed.publish"):
        await publish_composed_slide(asset.id, db_session)

    assert any("video widgets" in rec.message for rec in caplog.records)


@pytest.mark.asyncio
async def test_publish_mixed_image_and_video_uses_both_channels(
    db_session, mock_storage_and_dir
):
    _, tmp_path = mock_storage_and_dir
    img = await _make_image_asset(db_session, tmp_path, "pic.png")
    vid = await _make_video_asset(db_session, "clip.mp4")
    layout = empty_layout()
    layout.widgets.extend([
        WidgetInstance(
            id=uuid.uuid4(),
            type="media",
            cell=Cell(row=1, col=1, rowspan=2, colspan=4),
            config={"asset_id": str(img.id), "object_fit": "cover", "alt": ""},
            config_version=1,
        ),
        WidgetInstance(
            id=uuid.uuid4(),
            type="media",
            cell=Cell(row=3, col=1, rowspan=2, colspan=4),
            config={"asset_id": str(vid.id), "object_fit": "cover", "alt": ""},
            config_version=1,
        ),
    ])
    asset, cs = await _make_composed(db_session, layout=layout)

    await publish_composed_slide(asset.id, db_session)

    bundle_html = (tmp_path / asset.filename).read_text()
    assert "data:image/png;base64," in bundle_html
    assert "/assets/videos/clip.mp4" in bundle_html

    await db_session.refresh(cs)
    assert {str(x) for x in cs.bundle_source_asset_ids} == {str(img.id), str(vid.id)}


# --- Slideshow-in-media-widget publish tests -----------------------------


async def _make_slideshow_asset(db_session, members, filename="show.slideshow") -> Asset:
    """Seed a SLIDESHOW container asset + ordered member slides.

    ``members`` is a list of (source_asset, duration_ms, transition,
    transition_ms).
    """
    ss = Asset(
        filename=filename,
        asset_type=AssetType.SLIDESHOW,
        size_bytes=0,
        checksum="",
    )
    db_session.add(ss)
    await db_session.flush()
    for idx, (src, dur, trans, trans_ms) in enumerate(members):
        db_session.add(SlideshowSlide(
            slideshow_asset_id=ss.id,
            source_asset_id=src.id,
            position=idx,
            duration_ms=dur,
            play_to_end=False,
            transition=trans,
            transition_ms=trans_ms,
        ))
    await db_session.flush()
    return ss


@pytest.mark.asyncio
async def test_publish_slideshow_member_expands_image_sources(
    db_session, mock_storage_and_dir
):
    _, tmp_path = mock_storage_and_dir
    img1 = await _make_image_asset(db_session, tmp_path, "a.png")
    img2 = await _make_image_asset(db_session, tmp_path, "b.png")
    ss = await _make_slideshow_asset(
        db_session,
        [(img1, 4000, "fade", 600), (img2, 5000, "cut", 600)],
    )
    asset, cs = await _make_composed(db_session, layout=_media_layout(ss.id))

    await publish_composed_slide(asset.id, db_session)

    bundle_html = (tmp_path / asset.filename).read_text()
    # Two stacked slides cycling client-side — both image sources inlined.
    assert bundle_html.count("data:image/png;base64,") == 2
    assert "cw-ss-slide" in bundle_html

    await db_session.refresh(cs)
    sources = {str(x) for x in cs.bundle_source_asset_ids}
    # Per-slide SOURCE ids are tracked, NOT the slideshow container id.
    assert sources == {str(img1.id), str(img2.id)}
    assert str(ss.id) not in sources


@pytest.mark.asyncio
async def test_publish_slideshow_member_mixed_image_video(
    db_session, mock_storage_and_dir
):
    _, tmp_path = mock_storage_and_dir
    img = await _make_image_asset(db_session, tmp_path, "a.png")
    vid = await _make_video_asset(db_session, "clip.mp4")
    ss = await _make_slideshow_asset(
        db_session,
        [(img, 4000, "cut", 600), (vid, 6000, "fade", 600)],
    )
    asset, _ = await _make_composed(db_session, layout=_media_layout(ss.id))

    await publish_composed_slide(asset.id, db_session)

    bundle_html = (tmp_path / asset.filename).read_text()
    assert "data:image/png;base64," in bundle_html
    assert "/assets/videos/clip.mp4" in bundle_html


@pytest.mark.asyncio
async def test_publish_rejects_empty_slideshow(db_session, mock_storage_and_dir):
    ss = await _make_slideshow_asset(db_session, [])
    asset, _ = await _make_composed(db_session, layout=_media_layout(ss.id))

    with pytest.raises(PublishError, match="has no slides"):
        await publish_composed_slide(asset.id, db_session)


@pytest.mark.asyncio
async def test_publish_rejects_slideshow_with_non_media_member(
    db_session, mock_storage_and_dir
):
    web = Asset(
        filename="page.url",
        asset_type=AssetType.WEBPAGE,
        size_bytes=0,
        checksum="",
    )
    db_session.add(web)
    await db_session.flush()
    ss = await _make_slideshow_asset(db_session, [(web, 4000, "cut", 600)])
    asset, _ = await _make_composed(db_session, layout=_media_layout(ss.id))

    with pytest.raises(PublishError, match="can only cycle IMAGE and VIDEO"):
        await publish_composed_slide(asset.id, db_session)
