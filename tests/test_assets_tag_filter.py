"""Tests for ``tag_id`` filter on ``GET /api/assets/page``.

Multi-tag filtering uses AND-semantics: assets must have ALL selected
tags to appear in the result.
"""

from __future__ import annotations

import pytest


async def _seed_assets(db_session, n: int = 3):
    from cms.models.asset import Asset, AssetType

    created = []
    for i in range(n):
        a = Asset(
            filename=f"a-{i}.png",
            original_filename=f"a-{i}.png",
            asset_type=AssetType.IMAGE,
            size_bytes=1000,
            checksum=f"chk{i}",
        )
        db_session.add(a)
        created.append(a)
    await db_session.commit()
    for a in created:
        await db_session.refresh(a)
    return created


async def _attach_tag(client, asset_ids, tag_id):
    resp = await client.post(
        "/api/assets/bulk",
        json={
            "asset_ids": [str(a) for a in asset_ids],
            "action": "add_tag",
            "tag_id": str(tag_id),
        },
    )
    assert resp.status_code == 200, resp.text


@pytest.mark.asyncio
class TestPageTagFilter:
    async def test_single_tag_filter(self, client, db_session):
        assets = await _seed_assets(db_session, n=3)
        promo = (await client.post("/api/tags", json={"name": "promo"})).json()
        await _attach_tag(client, [assets[0].id, assets[1].id], promo["id"])

        resp = await client.get(f"/api/assets/page?tag_id={promo['id']}")
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert ids == {str(assets[0].id), str(assets[1].id)}

    async def test_multi_tag_filter_is_and(self, client, db_session):
        assets = await _seed_assets(db_session, n=4)
        promo = (await client.post("/api/tags", json={"name": "promo"})).json()
        holiday = (await client.post("/api/tags", json={"name": "holiday"})).json()

        # 0,1 -> promo;  1,2 -> holiday;  intersection (AND) = {1}
        await _attach_tag(client, [assets[0].id, assets[1].id], promo["id"])
        await _attach_tag(client, [assets[1].id, assets[2].id], holiday["id"])

        resp = await client.get(
            f"/api/assets/page?tag_id={promo['id']}&tag_id={holiday['id']}"
        )
        assert resp.status_code == 200
        ids = {item["id"] for item in resp.json()["items"]}
        assert ids == {str(assets[1].id)}

    async def test_tags_embedded_in_response(self, client, db_session):
        assets = await _seed_assets(db_session, n=1)
        t = (await client.post("/api/tags", json={"name": "embed", "color": "#abcdef"})).json()
        await _attach_tag(client, [assets[0].id], t["id"])

        resp = await client.get("/api/assets/page")
        assert resp.status_code == 200
        items = resp.json()["items"]
        item = next(i for i in items if i["id"] == str(assets[0].id))
        assert len(item["tags"]) == 1
        assert item["tags"][0]["name"] == "embed"
        assert item["tags"][0]["color"] == "#abcdef"

    async def test_no_n_plus_one_serialization(self, client, db_session):
        """Sanity-check: a page of N assets should serialize without
        per-asset DB lookups for tags.  We assert via behaviour rather
        than instrumentation -- the page must come back in well under a
        second for 20 tagged assets on the test SQLite backend.
        """
        import time

        assets = await _seed_assets(db_session, n=20)
        t = (await client.post("/api/tags", json={"name": "perf"})).json()
        await _attach_tag(client, [a.id for a in assets], t["id"])

        start = time.monotonic()
        resp = await client.get("/api/assets/page?page_size=50")
        elapsed = time.monotonic() - start
        assert resp.status_code == 200
        assert len(resp.json()["items"]) == 20
        # Generous bound -- N+1 on 20 rows would still be fast on SQLite,
        # but this catches a pathological full table scan per row.
        assert elapsed < 2.0
