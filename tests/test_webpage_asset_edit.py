"""Tests for editing webpage-asset URLs via PATCH /api/assets/{id}.

Companion to the create path in `create_webpage_asset`. Shares the
`_validate_webpage_url` helper, so both endpoints get the same
SSRF / scheme / hostname guarantees.
"""

import pytest

from cms.models.asset import Asset, AssetType


async def _seed_webpage(db_session, url="https://example.com/original"):
    asset = Asset(
        filename="example.com",
        asset_type=AssetType.WEBPAGE,
        size_bytes=0,
        checksum="",
        url=url,
    )
    db_session.add(asset)
    await db_session.commit()
    return asset


async def _seed_video(db_session):
    asset = Asset(
        filename="video.mp4",
        asset_type=AssetType.VIDEO,
        size_bytes=5000,
        checksum="abc",
    )
    db_session.add(asset)
    await db_session.commit()
    return asset


@pytest.mark.asyncio
class TestWebpageAssetEdit:

    async def test_update_url_succeeds_for_webpage(self, client, db_session):
        asset = await _seed_webpage(db_session)
        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"url": "https://example.com/new-path"},
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["url"] == "https://example.com/new-path"

        await db_session.refresh(asset)
        assert asset.url == "https://example.com/new-path"

    async def test_update_url_trims_whitespace(self, client, db_session):
        asset = await _seed_webpage(db_session)
        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"url": "  https://example.com/trimmed  "},
        )
        assert resp.status_code == 200
        assert resp.json()["url"] == "https://example.com/trimmed"

    async def test_update_url_rejected_for_non_webpage(self, client, db_session):
        asset = await _seed_video(db_session)
        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"url": "https://example.com/nope"},
        )
        assert resp.status_code == 400
        assert "webpage" in resp.json()["detail"].lower()

    async def test_update_url_rejects_empty(self, client, db_session):
        asset = await _seed_webpage(db_session)
        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"url": "   "},
        )
        assert resp.status_code == 400
        assert "required" in resp.json()["detail"].lower()

    async def test_update_url_rejects_bad_scheme(self, client, db_session):
        asset = await _seed_webpage(db_session)
        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"url": "javascript:alert(1)"},
        )
        assert resp.status_code == 400

    async def test_update_url_rejects_loopback(self, client, db_session):
        asset = await _seed_webpage(db_session)
        # 127.0.0.1 has a dot so it passes the hostname shape check and
        # lands in the loopback block explicitly.
        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"url": "http://127.0.0.1/admin"},
        )
        assert resp.status_code == 400
        assert "loopback" in resp.json()["detail"].lower()

    async def test_update_url_rejects_hostname_without_dot(self, client, db_session):
        asset = await _seed_webpage(db_session)
        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"url": "http://intranet/page"},
        )
        assert resp.status_code == 400

    async def test_update_url_too_long(self, client, db_session):
        asset = await _seed_webpage(db_session)
        long_path = "a" * 2100
        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"url": f"https://example.com/{long_path}"},
        )
        assert resp.status_code == 400
        assert "too long" in resp.json()["detail"].lower()

    async def test_update_url_audit_logged(self, client, db_session):
        """URL changes must land in the audit log so operators can answer
        'who changed the webpage to X and when?'."""
        from cms.models.audit_log import AuditLog
        from sqlalchemy import select

        asset = await _seed_webpage(db_session, url="https://example.com/before")
        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={"url": "https://example.com/after"},
        )
        assert resp.status_code == 200

        res = await db_session.execute(
            select(AuditLog)
            .where(AuditLog.resource_type == "asset")
            .where(AuditLog.resource_id == str(asset.id))
            .where(AuditLog.action == "asset.update")
        )
        rows = res.scalars().all()
        assert rows, "expected at least one asset.update audit row"
        changes = rows[-1].details.get("changes", {})
        assert "url" in changes
        assert changes["url"]["old"] == "https://example.com/before"
        assert changes["url"]["new"] == "https://example.com/after"

    async def test_update_url_and_display_name_together(self, client, db_session):
        asset = await _seed_webpage(db_session)
        resp = await client.patch(
            f"/api/assets/{asset.id}",
            json={
                "url": "https://example.com/new",
                "display_name": "My Page",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["url"] == "https://example.com/new"
        assert data["display_name"] == "My Page"
