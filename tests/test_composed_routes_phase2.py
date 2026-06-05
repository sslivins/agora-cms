"""Phase 2 tests for the composed-slide write/publish routes and the
UI builder routes.

Scope:

* ``POST /composed/`` — admin create round-trip; missing-name 400.
* ``PATCH /composed/{id}/layout`` — sets ``is_draft=True`` even after
  publish; invalid layout shape returns 422 (not 500).
* ``POST /composed/{id}/publish`` — empty-layout returns a friendly 422.
* Auth: unauth user gets redirected (303/302) to /login on UI routes
  and 401 on the JSON API routes.
* UI: ``GET /assets/new/composed`` renders for an authed user; the
  editor route renders for an existing composed asset and 303s to
  /assets for a missing one.
"""

from __future__ import annotations

import json
import re
import uuid
from pathlib import Path

import pytest

from cms.composed.schema import (
    SCHEMA_VERSION,
    Cell,
    Layout,
    WidgetInstance,
    empty_layout,
)
from cms.models.asset import Asset, AssetType
from cms.models.composed_slide import ComposedSlide

EDITOR_TEMPLATE = (
    Path(__file__).resolve().parents[1]
    / "cms"
    / "templates"
    / "composed_editor.html"
)


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


# ─────────────────────────── API: create ───────────────────────────


@pytest.mark.asyncio
class TestComposedCreate:
    async def test_admin_can_create(self, client):
        resp = await client.post(
            "/composed/", json={"name": "Lobby welcome"}
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["display_name"] == "Lobby welcome"
        assert body["edit_url"].startswith("/assets/")
        assert body["edit_url"].endswith("/composed")
        # The id round-trips as a UUID.
        uuid.UUID(body["id"])

    async def test_missing_name_is_400(self, client):
        resp = await client.post("/composed/", json={})
        assert resp.status_code == 400

    async def test_unauth_is_401(self, unauthed_client):
        resp = await unauthed_client.post("/composed/", json={"name": "x"})
        # The API is JSON, so it does NOT redirect — it returns 401.
        assert resp.status_code in (401, 403)


# ─────────────────────────── API: patch ────────────────────────────


@pytest.mark.asyncio
class TestComposedPatch:
    async def test_invalid_layout_shape_is_422_not_500(self, client, db_session):
        asset, _ = await _make_composed(db_session)
        # Send a totally bogus body — not a Layout shape.
        resp = await client.patch(
            f"/composed/{asset.id}/layout", json={"hello": "world"}
        )
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, dict)
        assert detail["error"] in ("invalid_layout_shape", "invalid_layout")

    async def test_patch_after_publish_re_drafts(self, client, db_session):
        """Once published (is_draft=False), a PATCH must set is_draft=True
        again so the device-facing bundle is flagged stale until republished.
        """
        layout = empty_layout()
        layout.widgets.append(_text_widget("hi"))
        asset, cs = await _make_composed(db_session, layout=layout, is_draft=False)
        assert cs.is_draft is False

        new_layout = empty_layout()
        new_layout.widgets.append(_text_widget("updated"))

        resp = await client.patch(
            f"/composed/{asset.id}/layout",
            json=new_layout.model_dump(mode="json"),
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["is_draft"] is True

        await db_session.refresh(cs)
        assert cs.is_draft is True


# ─────────────────────────── API: publish ──────────────────────────


@pytest.mark.asyncio
class TestComposedPublish:
    async def test_empty_layout_returns_friendly_422(self, client, db_session):
        asset, _ = await _make_composed(db_session)  # default empty layout
        resp = await client.post(f"/composed/{asset.id}/publish")
        assert resp.status_code == 422
        detail = resp.json()["detail"]
        assert isinstance(detail, dict)
        assert detail["error"] == "empty_layout"
        assert "widget" in detail["message"].lower()

    async def test_publish_missing_asset_404(self, client):
        resp = await client.post(f"/composed/{uuid.uuid4()}/publish")
        assert resp.status_code == 404


# ─────────────────────────── UI routes ─────────────────────────────


@pytest.mark.asyncio
class TestComposedUI:
    async def test_new_page_renders_for_authed_user(self, client):
        resp = await client.get("/assets/new/composed")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        # Some breadcrumb of the form is present.
        text = resp.text.lower()
        assert "name" in text

    async def test_new_page_seeds_schema_valid_layout(self, client):
        # Regression: the create-mode page seeds ``let LAYOUT = {...}``
        # straight from the server context. Earlier this hard-coded a legacy
        # ``canvas: {w, h}`` literal, so the FIRST save of a brand-new slide
        # PATCHed ``{w, h}`` and was rejected 422 ``extra_forbidden``. The JS
        # ``|| {width,height}`` fallback (covered by TestEditorDefaultsMatch
        # Schema) never fires here because the seeded canvas is already
        # truthy — so the server seed itself must use schema keys.
        resp = await client.get("/assets/new/composed")
        assert resp.status_code == 200
        m = re.search(r"let LAYOUT = (\{.*\});", resp.text)
        assert m, "could not find seeded `let LAYOUT` object in new page"
        seeded = json.loads(m.group(1))
        assert seeded["canvas"] == {"width": 1920, "height": 1080}, seeded
        assert "w" not in seeded["canvas"]
        assert "h" not in seeded["canvas"]
        # Exactly what the editor PATCHes before any widget is dropped.
        Layout.model_validate(seeded)

    async def test_editor_renders_for_existing_composed(self, client, db_session):
        asset, _ = await _make_composed(db_session)
        resp = await client.get(f"/assets/{asset.id}/composed")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    async def test_editor_redirects_for_missing_asset(self, client):
        resp = await client.get(
            f"/assets/{uuid.uuid4()}/composed", follow_redirects=False
        )
        assert resp.status_code in (302, 303)
        assert resp.headers["location"] == "/assets"

    async def test_media_assets_carry_thumbnail_url_key(self, client, db_session):
        # The editor canvas renders live media previews from a
        # ``thumbnail_url`` field injected per media asset. The key must
        # always be present (value may be null when no ready thumbnail
        # variant exists) so the client can fall back to a type icon.
        img = Asset(
            filename=f"pic-{uuid.uuid4()}.jpg",
            display_name="Lobby photo",
            asset_type=AssetType.IMAGE,
            size_bytes=10,
            checksum="abc",
            is_global=True,
        )
        db_session.add(img)
        await db_session.commit()

        resp = await client.get("/assets/new/composed")
        assert resp.status_code == 200
        assert "thumbnail_url" in resp.text
        assert str(img.id) in resp.text

    async def test_editor_ships_live_preview_renderer(self, client, db_session):
        asset, _ = await _make_composed(db_session)
        resp = await client.get(f"/assets/{asset.id}/composed")
        assert resp.status_code == 200
        # Live in-editor widget previews replaced the old generic label.
        assert "renderWidgetContent" in resp.text
        assert "container-type: inline-size" in resp.text

    async def test_unauth_ui_redirects_to_login(self, unauthed_client):
        resp = await unauthed_client.get(
            "/assets/new/composed", follow_redirects=False
        )
        # The UI requires auth — accept either a redirect or a 401.
        assert resp.status_code in (302, 303, 401)


# ───────────────── Editor ↔ schema contract (regression) ─────────────────


def _js_object_literal_to_dict(literal: str) -> dict:
    """Convert a simple JS object literal (identifier keys, int/string
    values) into a Python dict via JSON. Only handles the flat literals
    the editor's ``ensureLayoutShape`` emits."""
    quoted = re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', literal)
    return json.loads(quoted)


def _extract_default(field: str) -> dict:
    """Pull the ``LAYOUT.<field> = LAYOUT.<field> || {...}`` default
    object literal out of the editor template."""
    src = EDITOR_TEMPLATE.read_text(encoding="utf-8")
    m = re.search(
        rf"LAYOUT\.{re.escape(field)}\s*=\s*LAYOUT\.{re.escape(field)}\s*\|\|\s*(\{{.*?\}})",
        src,
    )
    assert m, f"could not find editor default literal for LAYOUT.{field}"
    return _js_object_literal_to_dict(m.group(1))


class TestEditorDefaultsMatchSchema:
    """The empty-slide defaults the editor JS injects via
    ``ensureLayoutShape`` MUST validate against the Pydantic ``Layout``
    contract. This guards the canvas ``{w,h}`` vs ``{width,height}``
    regression (a mismatch made every save fail with 422
    ``extra_forbidden``) and any future drift in the editor's grid /
    background / canvas key names.
    """

    def test_editor_canvas_default_uses_schema_keys(self):
        canvas = _extract_default("canvas")
        # Direct guard against the `{w, h}` regression.
        assert set(canvas) == {"width", "height"}, canvas

    def test_editor_empty_layout_validates_against_schema(self):
        payload = {
            "schema_version": SCHEMA_VERSION,
            "grid": _extract_default("grid"),
            "canvas": _extract_default("canvas"),
            "background": _extract_default("background"),
            "widgets": [],
        }
        # Must not raise — this is exactly what the editor PATCHes on a
        # brand-new slide before any widget is dropped.
        Layout.model_validate(payload)


@pytest.mark.asyncio
class TestEditorEmptyLayoutSaves:
    """End-to-end: the literal empty-slide shape the editor emits is
    accepted by the save endpoint (200, not 422)."""

    async def test_patch_editor_empty_layout_returns_200(
        self, client, db_session
    ):
        asset, _ = await _make_composed(db_session)
        body = {
            "schema_version": SCHEMA_VERSION,
            "grid": _extract_default("grid"),
            "canvas": _extract_default("canvas"),
            "background": _extract_default("background"),
            "widgets": [],
        }
        resp = await client.patch(f"/composed/{asset.id}/layout", json=body)
        assert resp.status_code == 200, resp.text
