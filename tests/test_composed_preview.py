"""Phase 1A tests for the composed slide live preview endpoint."""

from __future__ import annotations

import uuid

import pytest

from cms.composed.schema import Cell, Layout, WidgetInstance, empty_layout
from cms.models.asset import Asset, AssetType
from cms.models.composed_slide import ComposedSlide


def _text_layout(text: str = "hello preview") -> Layout:
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


async def _make_composed(
    db_session, *, layout: Layout | None = None
) -> tuple[Asset, ComposedSlide]:
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
    await db_session.commit()
    return asset, cs


@pytest.mark.asyncio
class TestComposedPreview:
    async def test_404_when_asset_missing(self, client):
        resp = await client.get(f"/composed/{uuid.uuid4()}/preview")
        assert resp.status_code == 404

    async def test_404_when_wrong_asset_type(self, client, db_session):
        asset = Asset(
            filename="not_composed.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=100,
            checksum="abc",
        )
        db_session.add(asset)
        await db_session.commit()

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 404

    async def test_404_when_composed_row_missing(self, client, db_session):
        # COMPOSED asset row exists but no ComposedSlide attached.
        asset = Asset(
            filename="orphan-composed",
            asset_type=AssetType.COMPOSED,
            size_bytes=0,
            checksum="",
        )
        db_session.add(asset)
        await db_session.commit()

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 404

    async def test_happy_path_returns_html_with_widget_content(
        self, client, db_session
    ):
        asset, _cs = await _make_composed(
            db_session, layout=_text_layout("preview content xyz")
        )

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/html")
        body = resp.text
        assert "preview content xyz" in body
        # Bundle is a complete HTML document.
        assert "<html" in body.lower()

    async def test_csp_header_is_locked_down(self, client, db_session):
        asset, _cs = await _make_composed(db_session)

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 200
        csp = resp.headers.get("content-security-policy", "")
        assert "default-src 'none'" in csp
        assert "frame-ancestors 'self'" in csp
        # No external script/style/img origins allowed.
        assert "http:" not in csp
        assert "https:" not in csp

    async def test_no_cache_header_set(self, client, db_session):
        asset, _cs = await _make_composed(db_session)

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 200
        assert resp.headers.get("cache-control") == "no-store"

    async def test_invalid_layout_json_returns_422(self, client, db_session):
        asset = Asset(
            filename="bad-layout",
            asset_type=AssetType.COMPOSED,
            size_bytes=0,
            checksum="",
        )
        db_session.add(asset)
        await db_session.flush()
        # layout_json is not a valid Layout shape.
        cs = ComposedSlide(asset_id=asset.id, layout_json={"not": "a layout"})
        db_session.add(cs)
        await db_session.commit()

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 422


# ─────────────────────────────────────────────────────────────────────
# Media-asset preview (image + video) — regression for the
# missing_asset_loader 422 (composed slide referencing a media asset
# could not be previewed at all).
# ─────────────────────────────────────────────────────────────────────


def _media_layout(asset_id: uuid.UUID, *, widget_type: str = "media") -> Layout:
    layout = empty_layout()
    layout.widgets.append(
        WidgetInstance(
            id=uuid.uuid4(),
            type=widget_type,
            cell=Cell(row=1, col=1, rowspan=2, colspan=4),
            config={"asset_id": str(asset_id), "object_fit": "cover", "alt": ""},
            config_version=1,
        )
    )
    return layout


def _storage_dir(tmp_path):
    # Mirrors the conftest ``app`` fixture: asset_storage_path = tmp_path/"assets".
    d = tmp_path / "assets"
    d.mkdir(parents=True, exist_ok=True)
    return d


# A 1x1 PNG (valid header is enough for preview — we only inline bytes).
_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x06\x00\x00\x00\x1f\x15\xc4\x89"
)


@pytest.mark.asyncio
class TestComposedPreviewMedia:
    async def _make_composed_with(self, db_session, layout: Layout):
        asset = Asset(
            filename=f"composed-{uuid.uuid4()}",
            asset_type=AssetType.COMPOSED,
            size_bytes=0,
            checksum="",
        )
        db_session.add(asset)
        await db_session.flush()
        cs = ComposedSlide(
            asset_id=asset.id, layout_json=layout.model_dump(mode="json")
        )
        db_session.add(cs)
        await db_session.commit()
        return asset

    async def test_media_widget_image_inlines_data_uri(
        self, client, db_session, tmp_path
    ):
        storage = _storage_dir(tmp_path)
        (storage / "pic.png").write_bytes(_PNG_BYTES)
        img = Asset(
            filename="pic.png",
            asset_type=AssetType.IMAGE,
            size_bytes=len(_PNG_BYTES),
            checksum="x",
        )
        db_session.add(img)
        await db_session.flush()
        asset = await self._make_composed_with(db_session, _media_layout(img.id))

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "data:image/png;base64," in body
        # No device-local sibling path should leak into a preview.
        assert "/assets/videos/" not in body

    async def test_media_widget_video_inlines_data_uri(
        self, client, db_session, tmp_path
    ):
        storage = _storage_dir(tmp_path)
        (storage / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        vid = Asset(
            filename="clip.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=12,
            checksum="y",
        )
        db_session.add(vid)
        await db_session.flush()
        asset = await self._make_composed_with(db_session, _media_layout(vid.id))

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 200, resp.text
        body = resp.text
        assert "<video " in body
        # Preview inlines the video as a data: URI so it renders in-browser
        # under the locked-down CSP (no device-local /assets path).
        assert "src=\"data:video/mp4;base64," in body
        assert "/assets/videos/" not in body

    async def test_preview_csp_unchanged_for_video(
        self, client, db_session, tmp_path
    ):
        storage = _storage_dir(tmp_path)
        (storage / "clip2.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        vid = Asset(
            filename="clip2.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=12,
            checksum="y",
        )
        db_session.add(vid)
        await db_session.flush()
        asset = await self._make_composed_with(db_session, _media_layout(vid.id))

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 200, resp.text
        csp = resp.headers.get("content-security-policy", "")
        assert "media-src data:" in csp
        assert "http:" not in csp
        assert "https:" not in csp

    async def test_image_widget_referencing_video_is_clean_422(
        self, client, db_session, tmp_path
    ):
        # ImageWidget can only render bytes (<img>); a VIDEO routed to it
        # must produce a clean 422, not a 500 RuntimeError.
        _storage_dir(tmp_path)
        vid = Asset(
            filename="badwidget.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=12,
            checksum="y",
        )
        db_session.add(vid)
        await db_session.flush()
        asset = await self._make_composed_with(
            db_session, _media_layout(vid.id, widget_type="image")
        )

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 422, resp.text

    async def test_missing_referenced_asset_is_422(
        self, client, db_session, tmp_path
    ):
        _storage_dir(tmp_path)
        asset = await self._make_composed_with(
            db_session, _media_layout(uuid.uuid4())
        )
        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 422, resp.text

    async def test_deleted_referenced_asset_is_422(
        self, client, db_session, tmp_path
    ):
        from datetime import datetime, timezone

        storage = _storage_dir(tmp_path)
        (storage / "gone.png").write_bytes(_PNG_BYTES)
        img = Asset(
            filename="gone.png",
            asset_type=AssetType.IMAGE,
            size_bytes=len(_PNG_BYTES),
            checksum="x",
            deleted_at=datetime.now(timezone.utc),
        )
        db_session.add(img)
        await db_session.flush()
        asset = await self._make_composed_with(db_session, _media_layout(img.id))

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 422, resp.text

    async def test_oversized_video_is_422(self, client, db_session, tmp_path):
        # Metadata says the video is far larger than the inline cap; the
        # endpoint must refuse before reading it into memory.
        _storage_dir(tmp_path)
        vid = Asset(
            filename="huge.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=64 * 1024 * 1024,
            checksum="y",
        )
        db_session.add(vid)
        await db_session.flush()
        asset = await self._make_composed_with(db_session, _media_layout(vid.id))

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 422, resp.text

    async def test_video_via_two_widgets_one_incapable_is_422(
        self, client, db_session, tmp_path
    ):
        # Same video asset declared by a media widget AND an image widget;
        # the image widget can't render video so the whole preview must
        # 422 (not slip through on the valid media declaration).
        storage = _storage_dir(tmp_path)
        (storage / "shared.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42")
        vid = Asset(
            filename="shared.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=12,
            checksum="y",
        )
        db_session.add(vid)
        await db_session.flush()
        layout = empty_layout()
        layout.widgets.append(
            WidgetInstance(
                id=uuid.uuid4(),
                type="media",
                cell=Cell(row=1, col=1, rowspan=2, colspan=4),
                config={"asset_id": str(vid.id), "object_fit": "cover", "alt": ""},
                config_version=1,
            )
        )
        layout.widgets.append(
            WidgetInstance(
                id=uuid.uuid4(),
                type="image",
                cell=Cell(row=3, col=1, rowspan=2, colspan=4),
                config={"asset_id": str(vid.id), "object_fit": "cover", "alt": ""},
                config_version=1,
            )
        )
        asset = await self._make_composed_with(db_session, layout)

        resp = await client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code == 422, resp.text

    async def test_unauthorized_user_cannot_preview(
        self, operator_client, client, db_session, tmp_path
    ):
        # A non-admin Operator with no group memberships can only see
        # global assets; a personal/un-shared composed slide must 403.
        _storage_dir(tmp_path)
        asset = await self._make_composed_with(db_session, _text_layout("secret"))

        resp = await operator_client.get(f"/composed/{asset.id}/preview")
        assert resp.status_code in (403, 404), resp.text
        # Admin can still see it (sanity).
        ok = await client.get(f"/composed/{asset.id}/preview")
        assert ok.status_code == 200, ok.text
