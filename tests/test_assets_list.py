"""Tests for the paginated asset listing endpoint ``GET /api/assets/page``.

The legacy flat ``GET /api/assets`` is exercised separately by
``test_assets.py``; this file is specifically about the asset-library
Phase-1 search / filter / pagination / sort surface.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest


def _utc(year: int, month: int, day: int) -> datetime:
    return datetime(year, month, day, tzinfo=timezone.utc)


async def _seed_assets(db_session, names_types):
    """Insert assets with a deterministic uploaded_at spread (1 day apart)."""
    from cms.models.asset import Asset, AssetType

    base = _utc(2026, 1, 1)
    created = []
    for i, (name, atype) in enumerate(names_types):
        a = Asset(
            filename=name,
            original_filename=name,
            display_name=name.rsplit(".", 1)[0],
            asset_type=atype,
            size_bytes=1000 + i * 100,
            checksum=f"chk{i:03d}",
            uploaded_at=base + timedelta(days=i),
            duration_seconds=float(10 + i) if atype == AssetType.VIDEO else None,
        )
        db_session.add(a)
        created.append(a)
    await db_session.commit()
    for a in created:
        await db_session.refresh(a)
    return created


@pytest.mark.asyncio
class TestAssetsPage:
    async def test_empty(self, client):
        resp = await client.get("/api/assets/page")
        assert resp.status_code == 200, resp.text
        data = resp.json()
        assert data == {"items": [], "next_cursor": None, "total_estimate": 0}

    async def test_returns_all_when_no_filters(self, client, db_session):
        from cms.models.asset import AssetType

        await _seed_assets(
            db_session,
            [
                ("video-a.mp4", AssetType.VIDEO),
                ("video-b.mp4", AssetType.VIDEO),
                ("image-a.png", AssetType.IMAGE),
            ],
        )
        resp = await client.get("/api/assets/page")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total_estimate"] == 3
        assert {a["filename"] for a in data["items"]} == {
            "video-a.mp4",
            "video-b.mp4",
            "image-a.png",
        }
        # Default order is -uploaded_at (newest first); seed inserted in order.
        assert data["items"][0]["filename"] == "image-a.png"
        assert data["items"][-1]["filename"] == "video-a.mp4"

    async def test_substring_search_matches_display_name(self, client, db_session):
        from cms.models.asset import AssetType

        await _seed_assets(
            db_session,
            [
                ("holiday-promo-2026.mp4", AssetType.VIDEO),
                ("birthday-promo.mp4", AssetType.VIDEO),
                ("logo.png", AssetType.IMAGE),
            ],
        )
        resp = await client.get("/api/assets/page", params={"q": "holid"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["filename"] == "holiday-promo-2026.mp4"

    async def test_substring_search_matches_description(self, client, db_session):
        """``q`` also matches the user-editable free-text description, so an
        asset is findable by notes even when its name doesn't contain the
        search term."""
        from cms.models.asset import AssetType

        created = await _seed_assets(
            db_session,
            [
                ("clip-001.mp4", AssetType.VIDEO),
                ("clip-002.mp4", AssetType.VIDEO),
            ],
        )
        created[0].description = "Footage from the Seattle waterfront gala"
        await db_session.commit()

        # Term appears only in the description, not in any name field.
        resp = await client.get("/api/assets/page", params={"q": "waterfront"})
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["filename"] == "clip-001.mp4"
        assert items[0]["description"] == "Footage from the Seattle waterfront gala"

    async def test_type_filter_repeatable(self, client, db_session):
        from cms.models.asset import AssetType

        await _seed_assets(
            db_session,
            [
                ("v.mp4", AssetType.VIDEO),
                ("i.png", AssetType.IMAGE),
                ("w.url", AssetType.WEBPAGE),
            ],
        )
        # Single type
        resp = await client.get("/api/assets/page", params=[("type", "image")])
        assert {a["filename"] for a in resp.json()["items"]} == {"i.png"}

        # Two types
        resp = await client.get(
            "/api/assets/page", params=[("type", "image"), ("type", "video")]
        )
        assert {a["filename"] for a in resp.json()["items"]} == {"i.png", "v.mp4"}

    async def test_bad_type_filter_rejected(self, client):
        resp = await client.get("/api/assets/page", params={"type": "movie"})
        assert resp.status_code == 400

    async def test_bad_order_rejected(self, client):
        resp = await client.get("/api/assets/page", params={"order": "bogus"})
        assert resp.status_code == 400

    async def test_order_by_size(self, client, db_session):
        from cms.models.asset import AssetType

        # _seed_assets gives sizes 1000, 1100, 1200 in insertion order.
        await _seed_assets(
            db_session,
            [
                ("a.png", AssetType.IMAGE),
                ("b.png", AssetType.IMAGE),
                ("c.png", AssetType.IMAGE),
            ],
        )
        resp = await client.get("/api/assets/page", params={"order": "size_bytes"})
        sizes = [a["size_bytes"] for a in resp.json()["items"]]
        assert sizes == sorted(sizes)

        resp = await client.get("/api/assets/page", params={"order": "-size_bytes"})
        sizes = [a["size_bytes"] for a in resp.json()["items"]]
        assert sizes == sorted(sizes, reverse=True)

    async def test_pagination_cursor_walks_full_set(self, client, db_session):
        from cms.models.asset import AssetType

        await _seed_assets(
            db_session,
            [(f"asset-{i:02d}.png", AssetType.IMAGE) for i in range(12)],
        )

        seen = []
        cursor = None
        for _ in range(20):  # safety bound
            params: dict = {"page_size": "5"}
            if cursor:
                params["cursor"] = cursor
            resp = await client.get("/api/assets/page", params=params)
            assert resp.status_code == 200
            data = resp.json()
            seen.extend(a["filename"] for a in data["items"])
            cursor = data["next_cursor"]
            if cursor is None:
                break

        assert sorted(seen) == sorted([f"asset-{i:02d}.png" for i in range(12)])
        assert len(seen) == 12

    async def test_uploaded_after_before_filters(self, client, db_session):
        from cms.models.asset import AssetType

        # Inserted across 5 days starting 2026-01-01.
        await _seed_assets(
            db_session,
            [(f"a{i}.png", AssetType.IMAGE) for i in range(5)],
        )
        resp = await client.get(
            "/api/assets/page",
            params={
                "uploaded_after": "2026-01-02T00:00:00+00:00",
                "uploaded_before": "2026-01-04T00:00:00+00:00",
            },
        )
        names = {a["filename"] for a in resp.json()["items"]}
        # Day-0 (Jan 1) and day-3+ (Jan 4 onward) excluded.
        assert names == {"a1.png", "a2.png"}

    async def test_total_estimate_reflects_filters(self, client, db_session):
        from cms.models.asset import AssetType

        await _seed_assets(
            db_session,
            [
                ("logo.png", AssetType.IMAGE),
                ("hero.png", AssetType.IMAGE),
                ("intro.mp4", AssetType.VIDEO),
            ],
        )
        resp = await client.get("/api/assets/page", params={"type": "image"})
        assert resp.json()["total_estimate"] == 2

    async def test_invalid_cursor_returns_400(self, client):
        resp = await client.get(
            "/api/assets/page", params={"cursor": "not-base64!!"}
        )
        assert resp.status_code == 400
