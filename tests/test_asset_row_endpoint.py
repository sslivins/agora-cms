"""Unit test for GET /api/assets/{id}/row — the HTML fragment endpoint
used by the assets page's cross-replica poller to swap a single row pair
without a full page reload (issue #87)."""

import pytest


def _make_upload(name="fragment.mp4"):
    return {"file": (name, b"hello world", "video/mp4")}


@pytest.mark.asyncio
class TestAssetRowEndpoint:
    async def test_row_returns_html_fragment(self, client):
        up = await client.post("/api/assets/upload", files=_make_upload("fragment.mp4"))
        assert up.status_code == 201
        asset_id = up.json()["id"]

        resp = await client.get(f"/api/assets/{asset_id}/row")
        assert resp.status_code == 200
        body = resp.text
        # Both rows of the pair must be present, both carry the asset id.
        assert f'data-asset-id="{asset_id}"' in body
        assert f'data-detail-for="{asset_id}"' in body
        # Filename should appear in the collapsed row.
        assert "fragment.mp4" in body

    async def test_row_unknown_id_404(self, client):
        resp = await client.get("/api/assets/00000000-0000-0000-0000-000000000000/row")
        assert resp.status_code == 404
