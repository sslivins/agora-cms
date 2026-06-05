"""Tests for the AI-assistant composed-slide HTTP routes (PR 1).

Covers the three endpoints that back the editor-embedded AI assistant:

* ``GET  /composed/widget-types``          — widget catalog introspection.
* ``GET  /composed/{id}/layout``           — friendly read-back of a draft.
* ``PUT  /composed/{id}/layout-friendly``  — friendly write path the LLM
  uses (server assigns/preserves UUIDs, pins the locked canvas/grid,
  then runs the exact same shape + semantic + asset-ACL validation as
  the canonical PATCH endpoint).

The friendly endpoints exist so the LLM never has to spell out the
locked canvas/grid or manage widget UUIDs — the source of the "extra
inputs not permitted" failures it would otherwise hit.
"""

from __future__ import annotations

import uuid

import pytest

from cms.composed.schema import (
    CANVAS_HEIGHT,
    CANVAS_WIDTH,
    GRID_COLS,
    GRID_ROWS,
    Cell,
    Layout,
    WidgetInstance,
    empty_layout,
)
from cms.models.asset import Asset, AssetType
from cms.models.composed_slide import ComposedSlide


def _text_widget(text: str = "hi") -> WidgetInstance:
    return WidgetInstance(
        id=uuid.uuid4(),
        type="text",
        cell=Cell(row=1, col=1, rowspan=1, colspan=4),
        config={"text": text, "font_size_px": 64},
        config_version=1,
    )


async def _make_composed(
    db_session, *, layout: Layout | None = None, is_draft: bool = True,
) -> tuple[Asset, ComposedSlide]:
    asset = Asset(
        filename=f"composed-{uuid.uuid4()}",
        display_name="Test composed",
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
    )
    db_session.add(asset)
    await db_session.flush()

    cs = ComposedSlide(
        asset_id=asset.id,
        layout_json=(layout or empty_layout()).model_dump(mode="json"),
        is_draft=is_draft,
    )
    db_session.add(cs)
    await db_session.commit()
    return asset, cs


# ─────────────────────── GET /composed/widget-types ─────────────────


@pytest.mark.asyncio
class TestWidgetTypes:
    async def test_returns_catalog_with_text_widget(self, client):
        resp = await client.get("/composed/widget-types")
        assert resp.status_code == 200, resp.text
        body = resp.json()
        types = body["widget_types"]
        assert isinstance(types, list) and types

        by_type = {w["type"]: w for w in types}
        assert "text" in by_type
        text = by_type["text"]
        # Each descriptor carries everything the LLM needs to place a
        # widget without inventing a type or config field.
        for key in (
            "type",
            "display_name",
            "config_version",
            "references_asset",
            "required_fields",
            "default_config",
            "config_schema",
        ):
            assert key in text, f"missing {key!r} in widget descriptor"
        assert text["references_asset"] is False
        assert isinstance(text["config_schema"], dict)

    async def test_media_widget_flagged_as_referencing_asset(self, client):
        resp = await client.get("/composed/widget-types")
        by_type = {w["type"]: w for w in resp.json()["widget_types"]}
        # The media widget takes an asset_id, so the AI knows it must
        # supply a real asset reference.
        assert by_type["media"]["references_asset"] is True

    async def test_unauth_is_401(self, unauthed_client):
        resp = await unauthed_client.get("/composed/widget-types")
        assert resp.status_code in (401, 403)


# ─────────────────────── GET /composed/{id}/layout ──────────────────


@pytest.mark.asyncio
class TestGetLayout:
    async def test_round_trips_widget_ids(self, client, db_session):
        w = _text_widget("hello")
        layout = empty_layout()
        layout.widgets = [w]
        asset, _ = await _make_composed(db_session, layout=layout)

        resp = await client.get(f"/composed/{asset.id}/layout")
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["id"] == str(asset.id)
        assert body["is_draft"] is True
        assert body["canvas"] == {"width": CANVAS_WIDTH, "height": CANVAS_HEIGHT}
        assert body["grid"] == {"rows": GRID_ROWS, "cols": GRID_COLS}
        assert len(body["widgets"]) == 1
        out = body["widgets"][0]
        assert out["id"] == str(w.id)
        assert out["type"] == "text"
        assert out["row"] == 1 and out["col"] == 1
        assert out["colspan"] == 4
        assert out["config"]["text"] == "hello"

    async def test_missing_asset_is_404(self, client):
        resp = await client.get(f"/composed/{uuid.uuid4()}/layout")
        assert resp.status_code == 404

    async def test_unauth_is_401(self, unauthed_client, db_session):
        asset, _ = await _make_composed(db_session)
        resp = await unauthed_client.get(f"/composed/{asset.id}/layout")
        assert resp.status_code in (401, 403)


# ─────────────────── PUT /composed/{id}/layout-friendly ─────────────


@pytest.mark.asyncio
class TestPutLayoutFriendly:
    async def test_happy_path_assigns_uuid_and_sets_draft(
        self, client, db_session
    ):
        asset, _ = await _make_composed(db_session, is_draft=False)
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={
                "widgets": [
                    {"type": "text", "row": 2, "col": 3, "colspan": 5,
                     "config": {"text": "AI made this"}},
                ],
            },
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["id"] == str(asset.id)
        assert body["is_draft"] is True
        assert body["widget_count"] == 1

        # Read it back: a UUID was assigned and the cell persisted.
        read = await client.get(f"/composed/{asset.id}/layout")
        w = read.json()["widgets"][0]
        uuid.UUID(w["id"])
        assert w["type"] == "text"
        assert w["row"] == 2 and w["col"] == 3 and w["colspan"] == 5
        assert w["config"]["text"] == "AI made this"

    async def test_preserves_supplied_id(self, client, db_session):
        asset, _ = await _make_composed(db_session)
        wid = str(uuid.uuid4())
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={
                "widgets": [
                    {"id": wid, "type": "text", "row": 1, "col": 1,
                     "config": {"text": "keep my id"}},
                ],
            },
        )
        assert resp.status_code == 200, resp.text

        read = await client.get(f"/composed/{asset.id}/layout")
        assert read.json()["widgets"][0]["id"] == wid

    async def test_canvas_and_grid_are_pinned(self, client, db_session):
        """The friendly shape omits canvas/grid; the server pins the
        locked defaults so the LLM never has to (and can't) change them."""
        asset, _ = await _make_composed(db_session)
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={"widgets": [
                {"type": "text", "row": 1, "col": 1, "config": {"text": "x"}},
            ]},
        )
        assert resp.status_code == 200, resp.text
        read = await client.get(f"/composed/{asset.id}/layout")
        assert read.json()["canvas"] == {
            "width": CANVAS_WIDTH, "height": CANVAS_HEIGHT,
        }
        assert read.json()["grid"] == {"rows": GRID_ROWS, "cols": GRID_COLS}

    async def test_background_color_set_and_falls_back(
        self, client, db_session
    ):
        asset, _ = await _make_composed(db_session)
        # Explicit color is honoured.
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={
                "widgets": [
                    {"type": "text", "row": 1, "col": 1,
                     "config": {"text": "x"}},
                ],
                "background_color": "#123456",
            },
        )
        assert resp.status_code == 200, resp.text
        assert (await client.get(
            f"/composed/{asset.id}/layout"
        )).json()["background_color"] == "#123456"

        # Omitting it on a later write keeps the existing color.
        resp2 = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={"widgets": [
                {"type": "text", "row": 1, "col": 1, "config": {"text": "y"}},
            ]},
        )
        assert resp2.status_code == 200, resp2.text
        assert (await client.get(
            f"/composed/{asset.id}/layout"
        )).json()["background_color"] == "#123456"

    async def test_text_content_is_html_escaped(self, client, db_session):
        """User/AI text must never round-trip into the rendered bundle
        as raw HTML — the publish/preview path escapes it.  Here we pin
        that the friendly write stores the raw text (escaping happens at
        render time) and that the canonical validator accepted it."""
        asset, _ = await _make_composed(db_session)
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={"widgets": [
                {"type": "text", "row": 1, "col": 1,
                 "config": {"text": "<script>alert(1)</script>"}},
            ]},
        )
        assert resp.status_code == 200, resp.text
        # Stored verbatim; render-time html.escape is covered by the
        # widget/golden-master suite.
        stored = (await client.get(
            f"/composed/{asset.id}/layout"
        )).json()["widgets"][0]["config"]["text"]
        assert stored == "<script>alert(1)</script>"

    # ── failure paths ───────────────────────────────────────────────

    async def test_unknown_widget_type_is_422(self, client, db_session):
        asset, _ = await _make_composed(db_session)
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={"widgets": [
                {"type": "bogus", "row": 1, "col": 1, "config": {}},
            ]},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "invalid_layout"

    async def test_bad_widget_config_is_422(self, client, db_session):
        """Text widget requires non-empty ``text`` — empty config is a
        semantic 422 (caught by the per-widget ConfigSchema), not a 500.
        """
        asset, _ = await _make_composed(db_session)
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={"widgets": [
                {"type": "text", "row": 1, "col": 1, "config": {}},
            ]},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "invalid_layout"

    async def test_out_of_bounds_cell_is_422(self, client, db_session):
        asset, _ = await _make_composed(db_session)
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={"widgets": [
                {"type": "text", "row": 1, "col": GRID_COLS + 1,
                 "config": {"text": "x"}},
            ]},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "invalid_layout_shape"

    async def test_malformed_widget_id_is_422(self, client, db_session):
        asset, _ = await _make_composed(db_session)
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={"widgets": [
                {"id": "not-a-uuid", "type": "text", "row": 1, "col": 1,
                 "config": {"text": "x"}},
            ]},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "invalid_widget_id"

    async def test_widgets_not_a_list_is_422(self, client, db_session):
        asset, _ = await _make_composed(db_session)
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={"widgets": {"type": "text"}},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "invalid_widgets"

    async def test_referenced_asset_missing_is_400(self, client, db_session):
        """A media widget pointing at a non-existent asset is rejected
        by the referenced-asset ACL check (same 400 path as the
        canonical PATCH endpoint)."""
        missing = uuid.uuid4()
        asset, _ = await _make_composed(db_session)
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={"widgets": [
                {"type": "media", "row": 1, "col": 1,
                 "config": {"asset_id": str(missing)}},
            ]},
        )
        assert resp.status_code == 400, resp.text
        assert "not found" in resp.json()["detail"].lower()

    async def test_missing_asset_is_404(self, client):
        resp = await client.put(
            f"/composed/{uuid.uuid4()}/layout-friendly",
            json={"widgets": []},
        )
        assert resp.status_code == 404

    async def test_unauth_is_401(self, unauthed_client, db_session):
        asset, _ = await _make_composed(db_session)
        resp = await unauthed_client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={"widgets": []},
        )
        assert resp.status_code in (401, 403)


# ───────────── referenced-asset caller-visibility (cross-user leak) ──────────


async def _make_composed_owned_by(
    db_session, owner_id, *, layout: Layout | None = None,
) -> tuple[Asset, ComposedSlide]:
    """A personal composed slide (no groups, not global) owned by a user."""
    asset = Asset(
        filename=f"composed-{uuid.uuid4()}",
        display_name="Operator composed",
        asset_type=AssetType.COMPOSED,
        size_bytes=0,
        checksum="",
        uploaded_by_user_id=owner_id,
        is_global=False,
    )
    db_session.add(asset)
    await db_session.flush()
    cs = ComposedSlide(
        asset_id=asset.id,
        layout_json=(layout or empty_layout()).model_dump(mode="json"),
        is_draft=True,
    )
    db_session.add(cs)
    await db_session.commit()
    return asset, cs


async def _make_image_asset(
    db_session, *, owner_id=None, is_global: bool = False,
) -> Asset:
    asset = Asset(
        filename=f"img-{uuid.uuid4()}.png",
        display_name="Some image",
        asset_type=AssetType.IMAGE,
        size_bytes=10,
        checksum=uuid.uuid4().hex,
        uploaded_by_user_id=owner_id,
        is_global=is_global,
    )
    db_session.add(asset)
    await db_session.commit()
    return asset


@pytest.mark.asyncio
class TestReferencedAssetVisibility:
    """A non-admin must not be able to reference an asset they can't see.

    Regression guard for a cross-user private-asset leak: the friendly
    PUT (and the canonical PATCH it shares the validate/ACL helper with)
    used to check only that a referenced asset *existed*, not that the
    caller could *see* it. A user could therefore inline another user's
    private (unshared, non-global) asset into a composed-slide bundle by
    guessing its UUID. The existence query is now scoped to the caller's
    visible set, so an invisible asset is reported as "not found".
    """

    async def test_private_asset_of_other_user_is_not_found(
        self, operator_client, db_session,
    ):
        owner_id = operator_client.user_id
        composed, _ = await _make_composed_owned_by(db_session, owner_id)
        # Private image with no owner attribution, unshared + not global,
        # so the operator cannot see it — but it exists.
        hidden = await _make_image_asset(
            db_session, owner_id=None, is_global=False,
        )
        resp = await operator_client.put(
            f"/composed/{composed.id}/layout-friendly",
            json={"widgets": [
                {"type": "media", "row": 1, "col": 1,
                 "config": {"asset_id": str(hidden.id)}},
            ]},
        )
        assert resp.status_code == 400, resp.text
        assert "not found" in resp.json()["detail"].lower()

    async def test_global_asset_is_referenceable(
        self, operator_client, db_session,
    ):
        """Positive control: the visibility gate must not over-block a
        global asset the operator legitimately can see."""
        owner_id = operator_client.user_id
        composed, _ = await _make_composed_owned_by(db_session, owner_id)
        shared = await _make_image_asset(
            db_session, owner_id=None, is_global=True,
        )
        resp = await operator_client.put(
            f"/composed/{composed.id}/layout-friendly",
            json={"widgets": [
                {"type": "media", "row": 1, "col": 1,
                 "config": {"asset_id": str(shared.id)}},
            ]},
        )
        assert resp.status_code == 200, resp.text

    async def test_malformed_media_asset_id_is_422_not_500(
        self, client, db_session,
    ):
        """A garbage (non-UUID) asset_id is a clean 422, never a 500."""
        asset, _ = await _make_composed(db_session)
        resp = await client.put(
            f"/composed/{asset.id}/layout-friendly",
            json={"widgets": [
                {"type": "media", "row": 1, "col": 1,
                 "config": {"asset_id": "not-a-uuid"}},
            ]},
        )
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["error"] == "invalid_layout"
