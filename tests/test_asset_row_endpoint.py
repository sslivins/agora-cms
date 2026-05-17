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

    async def test_webpage_row_edit_url_onclick_is_html_safe(self, client):
        """Regression: the 'Edit URL' kebab item rendered for webpage assets
        must HTML-escape the embedded URL so the browser's HTML parser doesn't
        terminate the onclick="..." attribute at the URL's opening "
        character.

        Before the fix, the macro emitted::

            onclick="editWebpageUrl('<id>', "https://example.com/x")"

        which the browser parses as ``onclick="editWebpageUrl('<id>', "``
        followed by stray attributes — so the menu item's click handler is
        effectively empty and "Edit URL" does nothing (issue from the
        agora-cms assets page).

        The fix is to filter ``tojson`` through ``forceescape`` so the
        double-quotes become ``&#34;`` inside the attribute.
        """
        url = "https://example.com/some/path?a=1&b=2"
        resp = await client.post("/api/assets/webpage", json={"url": url, "name": "kebab-test"})
        assert resp.status_code == 201, resp.text
        asset_id = resp.json()["id"]

        row = await client.get(f"/api/assets/{asset_id}/row")
        assert row.status_code == 200
        body = row.text

        # The kebab Edit URL handler must be present.
        assert f"editWebpageUrl('{asset_id}'" in body, (
            "Edit URL kebab item missing from the rendered row -- "
            "did the macro's webpage branch or assets:write/is_owner gate change?"
        )

        # The URL must be HTML-encoded inside the onclick attribute. If the
        # raw " from tojson survives into the rendered HTML, the browser will
        # terminate the onclick attribute prematurely and the click handler
        # silently does nothing.
        bad = f'editWebpageUrl(\'{asset_id}\', "https://'
        assert bad not in body, (
            "Raw double-quote leaked into onclick attribute -- the Edit URL "
            "kebab item is inert. Use `| tojson | forceescape` so the quotes "
            "become &#34; in the attribute."
        )
        # Positive: the safe-escaped form should be there instead.
        assert f"editWebpageUrl('{asset_id}', &#34;https://" in body, (
            "Expected the URL inside onclick to be HTML-escaped via "
            "tojson | forceescape so the browser hands a valid JS expression "
            "to the click handler."
        )

