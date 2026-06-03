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
