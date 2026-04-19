"""Tests for asset sharing audit log entries (issue #176).

Verifies that:
  - ``POST /api/assets/{id}/share`` writes an ``asset.share`` audit entry
    with filename, target group info, and original uploader context.
  - ``DELETE /api/assets/{id}/share`` writes an ``asset.unshare`` audit
    entry with the same enrichment.
  - Both entries are visible through the ``GET /api/audit-log`` endpoint.
"""

from __future__ import annotations

import io
import uuid

import pytest


def _make_upload(filename: str, content: bytes = b"fakecontent"):
    return {"file": (filename, io.BytesIO(content), "application/octet-stream")}


async def _make_group(db_session, name: str | None = None) -> uuid.UUID:
    from cms.models.device import DeviceGroup

    group = DeviceGroup(
        id=uuid.uuid4(), name=name or f"grp-{uuid.uuid4().hex[:8]}"
    )
    db_session.add(group)
    await db_session.flush()
    await db_session.commit()
    return group.id


@pytest.mark.asyncio
class TestAssetSharingAuditLog:
    async def test_share_writes_audit_entry(self, client, db_session):
        upload = await client.post(
            "/api/assets/upload", files=_make_upload("sharing.mp4")
        )
        asset_id = upload.json()["id"]
        group_id = await _make_group(db_session, name="engineering")

        resp = await client.post(
            f"/api/assets/{asset_id}/share", params={"group_id": str(group_id)}
        )
        assert resp.status_code == 200

        audit = await client.get(
            "/api/audit-log", params={"action": "asset.share", "q": "sharing.mp4"}
        )
        assert audit.status_code == 200
        entries = audit.json()
        assert entries, "expected an asset.share audit entry"
        entry = entries[0]
        assert entry["action"] == "asset.share"
        assert entry["resource_id"] == asset_id
        details = entry.get("details") or {}
        assert details.get("asset_filename") == "sharing.mp4"
        assert details.get("group_id") == str(group_id)
        assert details.get("group_name") == "engineering"
        # Uploader context present (the test user uploaded the asset)
        assert "uploaded_by_user_id" in details
        assert "uploaded_by_email" in details

    async def test_unshare_writes_audit_entry(self, client, db_session):
        upload = await client.post(
            "/api/assets/upload", files=_make_upload("unshare-me.mp4")
        )
        asset_id = upload.json()["id"]
        group_id = await _make_group(db_session, name="ops")

        share_resp = await client.post(
            f"/api/assets/{asset_id}/share", params={"group_id": str(group_id)}
        )
        assert share_resp.status_code == 200
        unshare_resp = await client.delete(
            f"/api/assets/{asset_id}/share", params={"group_id": str(group_id)}
        )
        assert unshare_resp.status_code == 200

        audit = await client.get(
            "/api/audit-log", params={"action": "asset.unshare", "q": "unshare-me.mp4"}
        )
        assert audit.status_code == 200
        entries = audit.json()
        assert entries, "expected an asset.unshare audit entry"
        entry = entries[0]
        assert entry["action"] == "asset.unshare"
        assert entry["resource_id"] == asset_id
        details = entry.get("details") or {}
        assert details.get("asset_filename") == "unshare-me.mp4"
        assert details.get("group_id") == str(group_id)
        assert details.get("group_name") == "ops"
        assert "uploaded_by_user_id" in details
        assert "uploaded_by_email" in details
        # Description should use readable names, not bare UUIDs.
        assert "unshare-me.mp4" in entry["description"]
        assert "ops" in entry["description"]
