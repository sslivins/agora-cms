"""Membership reconciliation contract for the asset library poller.

The page decides whether to reconcile its rows from `membership_hash`, and
reconciles against `all_ids`. These tests pin the properties the client
relies on; if they drift, the library silently stops updating.
"""
import pytest

pytestmark = pytest.mark.asyncio


async def _make_assets(db_session, n, prefix="m"):
    from cms.models.asset import Asset, AssetType

    made = []
    for i in range(n):
        a = Asset(
            filename=f"{prefix}_{i}.mp4",
            asset_type=AssetType.VIDEO,
            size_bytes=10 + i,
            checksum=f"{prefix}{i}",
        )
        db_session.add(a)
        made.append(a)
    await db_session.commit()
    return made


class TestMembershipHash:
    async def test_stable_across_calls_when_nothing_changes(self, client, db_session):
        await _make_assets(db_session, 3)

        first = (await client.post("/api/assets/status", json={"ids": []})).json()
        second = (await client.post("/api/assets/status", json={"ids": []})).json()

        assert first["membership_hash"]
        assert first["membership_hash"] == second["membership_hash"]

    async def test_unaffected_by_which_ids_the_caller_asks_about(
        self, client, db_session
    ):
        """The hash describes the library, not the request.

        The page sends a growing id list as the user scrolls; if that changed
        the hash it would reconcile on every scroll.
        """
        made = await _make_assets(db_session, 3)

        none = (await client.post("/api/assets/status", json={"ids": []})).json()
        some = (
            await client.post(
                "/api/assets/status", json={"ids": [str(made[0].id)]}
            )
        ).json()

        assert none["membership_hash"] == some["membership_hash"]

    async def test_changes_when_an_asset_is_added(self, client, db_session):
        await _make_assets(db_session, 2)
        before = (await client.post("/api/assets/status", json={"ids": []})).json()

        await _make_assets(db_session, 1, prefix="extra")
        after = (await client.post("/api/assets/status", json={"ids": []})).json()

        assert before["membership_hash"] != after["membership_hash"]

    async def test_changes_when_an_add_and_a_delete_cancel_out(
        self, client, db_session
    ):
        """The case asset_count could never see.

        One asset in, one out: the count is identical on both sides, so the
        old tripwire reported "no change" while two rows were wrong.
        """
        made = await _make_assets(db_session, 3)
        before = (await client.post("/api/assets/status", json={"ids": []})).json()

        resp = await client.delete(f"/api/assets/{made[0].id}")
        assert resp.status_code in (200, 204)
        await _make_assets(db_session, 1, prefix="replacement")

        after = (await client.post("/api/assets/status", json={"ids": []})).json()

        assert after["asset_count"] == before["asset_count"], (
            "precondition: the count tripwire is blind here"
        )
        assert after["membership_hash"] != before["membership_hash"]


class TestAllIds:
    async def test_omitted_unless_requested(self, client, db_session):
        """It's the largest field in the payload and the page needs it only
        when the hash moved."""
        await _make_assets(db_session, 3)

        data = (await client.post("/api/assets/status", json={"ids": []})).json()

        assert "all_ids" not in data

    async def test_returned_when_requested(self, client, db_session):
        made = await _make_assets(db_session, 3)

        data = (
            await client.post(
                "/api/assets/status", json={"ids": [], "include_ids": True}
            )
        ).json()

        assert set(data["all_ids"]) == {str(a.id) for a in made}

    async def test_count_matches_the_id_list(self, client, db_session):
        """These came from separate queries once, and drifted: the count
        included soft-deleted rows while the list did not."""
        made = await _make_assets(db_session, 4)
        resp = await client.delete(f"/api/assets/{made[0].id}")
        assert resp.status_code in (200, 204)

        data = (
            await client.post(
                "/api/assets/status", json={"ids": [], "include_ids": True}
            )
        ).json()

        assert data["asset_count"] == len(data["all_ids"])
        assert str(made[0].id) not in data["all_ids"]

    async def test_excludes_deleted_assets(self, client, db_session):
        made = await _make_assets(db_session, 2)
        resp = await client.delete(f"/api/assets/{made[0].id}")
        assert resp.status_code in (200, 204)

        data = (
            await client.post(
                "/api/assets/status", json={"ids": [], "include_ids": True}
            )
        ).json()

        assert str(made[0].id) not in data["all_ids"]
        assert str(made[1].id) in data["all_ids"]

    async def test_ordered_newest_first_to_match_the_rendered_page(
        self, client, db_session
    ):
        """The reconciler treats the rendered rows as a prefix of this list.
        If the orders disagree it will insert rows the user has not scrolled
        to and drop ones they have."""
        await _make_assets(db_session, 5)

        data = (
            await client.post(
                "/api/assets/status", json={"ids": [], "include_ids": True}
            )
        ).json()
        page = await client.get("/assets")
        html = page.text

        positions = [html.find(f'data-asset-id="{i}"') for i in data["all_ids"]]
        rendered = [p for p in positions if p != -1]

        assert rendered == sorted(rendered), "payload order differs from the page"
