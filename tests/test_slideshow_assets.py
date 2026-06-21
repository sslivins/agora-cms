"""Tests for the slideshow virtual asset feature (Commit 1).

Covers:

* schema (FK cascade on slideshow delete; FK restrict on source delete;
  unique (slideshow, position))
* POST /api/assets/slideshow create + validation matrix
* GET / PUT /api/assets/{id}/slides round-trip
* ACL invariant (global + group-scoped)
* source-asset delete guard while slideshow references exist
* unshare / unmark-global guards on source assets
* audit log entries for create + replace
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from cms.models.asset import Asset, AssetType
from cms.models.audit_log import AuditLog
from cms.models.device import DeviceGroup
from cms.models.group_asset import GroupAsset
from cms.models.slideshow_slide import SlideshowSlide
from cms.models.user import User


def test_ken_burns_directions_model_matches_schema():
    """The DB CHECK constraint (shared model) must allow exactly the same
    direction tokens the Pydantic schema accepts.  Drift between the two is
    what caused the HTTP 500 on Ken Burns save (schema widened to the full
    zoom+pan grammar, DB CHECK left at the original 6 tokens).  Lock them
    together so the next person who adds a direction can't reintroduce it.
    """
    from cms.schemas.asset import KEN_BURNS_DIRECTIONS as SCHEMA_DIRS
    from shared.models.slideshow_slide import KEN_BURNS_DIRECTIONS as MODEL_DIRS

    assert tuple(MODEL_DIRS) == tuple(SCHEMA_DIRS)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # already-canonical pass through unchanged
        ("in", "in"),
        ("out_down_right", "out_down_right"),
        ("down", "down"),
        # separators don't matter
        ("out-down-right", "out_down_right"),
        ("out down right", "out_down_right"),
        ("out/down/right", "out_down_right"),
        ("OUT_DOWN_RIGHT", "out_down_right"),
        # word ORDER doesn't matter (the user's exact case)
        ("out-right-down", "out_down_right"),
        ("zoom out right down", "out_down_right"),
        ("right_down", "down_right"),
        ("up-left", "up_left"),
        ("left up", "up_left"),
        # filler words stripped
        ("zoom in", "in"),
        ("zoom out going up left", "out_up_left"),
        # legacy bare pan
        ("up right", "up_right"),
    ],
)
def test_normalize_effect_direction_canonicalizes(raw, expected):
    from cms.schemas.asset import (
        KEN_BURNS_DIRECTIONS,
        normalize_effect_direction,
    )

    out = normalize_effect_direction(raw)
    assert out == expected
    assert out in KEN_BURNS_DIRECTIONS


@pytest.mark.parametrize(
    "raw",
    [
        "diagonal",          # unknown token
        "up_down",           # two verticals — not a valid pan
        "left_right",        # two horizontals
        "in_out",            # contradictory zoom
        "sideways",          # gibberish
    ],
)
def test_normalize_effect_direction_passes_invalid_through(raw):
    """Un-normalizable input is returned unchanged so the validator still
    raises its descriptive error (rather than silently coercing)."""
    from cms.schemas.asset import (
        KEN_BURNS_DIRECTIONS,
        normalize_effect_direction,
    )

    out = normalize_effect_direction(raw)
    assert out == raw
    assert out not in KEN_BURNS_DIRECTIONS


# ── Helpers ──


async def _seed_image(db_session, *, filename="img.png", is_global=False):
    asset = Asset(
        filename=filename,
        asset_type=AssetType.IMAGE,
        size_bytes=1234,
        checksum="img-cs",
        is_global=is_global,
    )
    db_session.add(asset)
    await db_session.commit()
    await db_session.refresh(asset)
    return asset


async def _seed_video(db_session, *, filename="vid.mp4", is_global=False, duration=12.0):
    asset = Asset(
        filename=filename,
        asset_type=AssetType.VIDEO,
        size_bytes=99999,
        checksum="vid-cs",
        is_global=is_global,
        duration_seconds=duration,
    )
    db_session.add(asset)
    await db_session.commit()
    await db_session.refresh(asset)
    return asset


async def _seed_webpage(db_session, *, filename="page", is_global=False):
    asset = Asset(
        filename=filename,
        asset_type=AssetType.WEBPAGE,
        size_bytes=0,
        checksum="",
        url="https://example.com/x",
        is_global=is_global,
    )
    db_session.add(asset)
    await db_session.commit()
    await db_session.refresh(asset)
    return asset


async def _seed_group(db_session, name="group-a"):
    g = DeviceGroup(name=name)
    db_session.add(g)
    await db_session.commit()
    await db_session.refresh(g)
    return g


async def _share(db_session, asset, group):
    db_session.add(GroupAsset(asset_id=asset.id, group_id=group.id))
    await db_session.commit()


# ── Schema ──


@pytest.mark.asyncio
class TestSlideshowSchema:

    async def test_cascade_on_slideshow_delete(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "cascade-test",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 5000}],
            },
        )
        assert resp.status_code == 201, resp.text
        slideshow_id = uuid.UUID(resp.json()["id"])

        # Hard-delete via session bypassing soft-delete path (we just want to
        # exercise the FK CASCADE, not the API's soft-delete guard).
        ss = await db_session.get(Asset, slideshow_id)
        await db_session.delete(ss)
        await db_session.commit()

        rows = (await db_session.execute(
            select(SlideshowSlide).where(
                SlideshowSlide.slideshow_asset_id == slideshow_id
            )
        )).scalars().all()
        assert rows == []

    async def test_restrict_on_source_delete(self, client, db_session):
        """At the database level, deleting a source asset that's still
        referenced by a slide row must fail (FK ON DELETE RESTRICT).  The
        API layer surfaces this as 409 — see TestSourceDeleteGuard."""
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "restrict-test",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 5000}],
            },
        )
        assert resp.status_code == 201, resp.text

        await db_session.delete(img)
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()

    async def test_unique_slideshow_position(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "unique-pos",
                "slides": [
                    {"source_asset_id": str(img.id), "duration_ms": 1000},
                    {"source_asset_id": str(img.id), "duration_ms": 2000},
                ],
            },
        )
        assert resp.status_code == 201
        slideshow_id = uuid.UUID(resp.json()["id"])

        db_session.add(
            SlideshowSlide(
                slideshow_asset_id=slideshow_id,
                source_asset_id=img.id,
                position=0,  # duplicate
                duration_ms=999,
            )
        )
        with pytest.raises(IntegrityError):
            await db_session.commit()
        await db_session.rollback()


# ── Create endpoint ──


@pytest.mark.asyncio
class TestSlideshowCreate:

    async def test_create_happy_path(self, client, db_session):
        img = await _seed_image(db_session, filename="a.png", is_global=True)
        vid = await _seed_video(
            db_session, filename="b.mp4", is_global=True, duration=8.0
        )
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "Hello slideshow",
                "slides": [
                    {"source_asset_id": str(img.id), "duration_ms": 5000},
                    {
                        "source_asset_id": str(vid.id),
                        "duration_ms": 1000,
                        "play_to_end": True,
                    },
                ],
            },
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["asset_type"] == "slideshow"
        assert body["filename"] == "Hello slideshow"
        # play_to_end on video uses source duration (8s), not 1s configured
        assert body["duration_seconds"] == pytest.approx(5.0 + 8.0)
        # is_global=True since admin + no groups
        assert body["is_global"] is True

    async def test_allows_empty_slides_creates_draft(self, client, db_session):
        # A 0-slide slideshow is a valid draft (the AI assistant mints one
        # on a fresh page before adding any slides). It must create a 201.
        resp = await client.post(
            "/api/assets/slideshow",
            json={"name": "Empty draft", "slides": []},
        )
        assert resp.status_code == 201, resp.text
        body = resp.json()
        assert body["asset_type"] == "slideshow"
        assert body["filename"] == "Empty draft"
        assert body["duration_seconds"] == 0

    async def test_rejects_too_many_slides(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        slides = [
            {"source_asset_id": str(img.id), "duration_ms": 1000} for _ in range(51)
        ]
        resp = await client.post(
            "/api/assets/slideshow", json={"name": "x", "slides": slides}
        )
        assert resp.status_code == 400
        assert "50" in resp.json()["detail"]

    async def test_rejects_missing_name(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "  ",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 400

    async def test_rejects_missing_source(self, client, db_session):
        ghost = uuid.uuid4()
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [{"source_asset_id": str(ghost), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 404

    async def test_rejects_webpage_source(self, client, db_session):
        page = await _seed_webpage(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [{"source_asset_id": str(page.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 400
        detail = resp.json()["detail"].lower()
        assert "image, video and composed" in detail

    async def test_rejects_play_to_end_on_image(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [
                    {
                        "source_asset_id": str(img.id),
                        "duration_ms": 1000,
                        "play_to_end": True,
                    }
                ],
            },
        )
        assert resp.status_code == 400
        assert "play_to_end" in resp.json()["detail"]

    async def test_allows_noop_clip_on_image(self, client, db_session):
        # Every slide the builder serializes carries clip_start_ms:0 /
        # clip_duration_ms:null (the legacy whole-asset default). That no-op
        # must NOT trip the video-only clip guard on an image source.
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [
                    {
                        "source_asset_id": str(img.id),
                        "duration_ms": 1000,
                        "clip_start_ms": 0,
                        "clip_duration_ms": None,
                    }
                ],
            },
        )
        assert resp.status_code == 201, resp.text

    async def test_rejects_real_clip_on_image(self, client, db_session):
        # An actual clip (start > 0) on an image is still rejected.
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [
                    {
                        "source_asset_id": str(img.id),
                        "duration_ms": 1000,
                        "clip_start_ms": 5000,
                    }
                ],
            },
        )
        assert resp.status_code == 400
        assert "video clipping" in resp.json()["detail"]

    async def test_allows_noop_clip_on_unprobed_video(self, client, db_session):
        # A plain untrimmed video whose duration hasn't been probed yet
        # (duration_seconds is None) must still save -- the no-op clip
        # default must not trip the "still being processed" guard.
        vid = await _seed_video(db_session, is_global=True, duration=None)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [
                    {
                        "source_asset_id": str(vid.id),
                        "duration_ms": 1000,
                        "clip_start_ms": 0,
                        "clip_duration_ms": None,
                    }
                ],
            },
        )
        assert resp.status_code == 201, resp.text

    async def test_rejects_duration_too_small(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 100}],
            },
        )
        assert resp.status_code == 400  # caught + re-raised as 400 by endpoint

    async def test_rejects_duration_too_large(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [
                    {"source_asset_id": str(img.id), "duration_ms": 60 * 60 * 1000 + 1}
                ],
            },
        )
        assert resp.status_code == 400


# ── ACL invariant ──


@pytest.mark.asyncio
class TestSlideshowACL:

    async def test_global_slideshow_requires_global_sources(
        self, client, db_session
    ):
        # Source NOT global; admin no-group create => slideshow becomes global.
        img = await _seed_image(db_session, is_global=False)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 400
        assert "global" in resp.json()["detail"].lower()

    async def test_group_slideshow_requires_source_in_group(
        self, client, db_session
    ):
        g = await _seed_group(db_session, "g1")
        img = await _seed_image(db_session, is_global=False)
        # Source not shared with g.
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "group_ids": [str(g.id)],
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 400

    async def test_group_slideshow_succeeds_when_source_shared(
        self, client, db_session
    ):
        g = await _seed_group(db_session, "g2")
        img = await _seed_image(db_session, is_global=False)
        await _share(db_session, img, g)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "x",
                "group_ids": [str(g.id)],
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 201, resp.text
        assert resp.json()["is_global"] is False


# ── GET / PUT slides ──


@pytest.mark.asyncio
class TestSlideshowSlidesEndpoints:

    async def test_get_slides_returns_ordered_with_source_metadata(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="aa.png", is_global=True)
        vid = await _seed_video(
            db_session, filename="bb.mp4", is_global=True, duration=4.0
        )
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "g",
                "slides": [
                    {"source_asset_id": str(vid.id), "duration_ms": 1000},
                    {"source_asset_id": str(img.id), "duration_ms": 2000},
                ],
            },
        )
        assert create.status_code == 201, create.text
        sid = create.json()["id"]
        resp = await client.get(f"/api/assets/{sid}/slides")
        assert resp.status_code == 200
        body = resp.json()
        assert [s["position"] for s in body["slides"]] == [0, 1]
        assert body["slides"][0]["source_filename"] == "bb.mp4"
        assert body["slides"][0]["source_asset_type"] == "video"
        assert body["slides"][1]["source_filename"] == "aa.png"

    async def test_get_slides_404_for_non_slideshow(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.get(f"/api/assets/{img.id}/slides")
        assert resp.status_code == 400
        assert "slideshow" in resp.json()["detail"].lower()

    async def test_replace_slides_updates_duration_and_rows(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="r1.png", is_global=True)
        img2 = await _seed_image(db_session, filename="r2.png", is_global=True)
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "rep",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert create.status_code == 201
        sid = create.json()["id"]
        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={
                "slides": [
                    {"source_asset_id": str(img2.id), "duration_ms": 4000},
                    {"source_asset_id": str(img.id), "duration_ms": 1500},
                ]
            },
        )
        assert put.status_code == 200, put.text
        assert put.json()["slide_count"] == 2
        assert put.json()["duration_seconds"] == pytest.approx(5.5)

        # And the asset row reflects it
        ss = await db_session.get(Asset, uuid.UUID(sid))
        await db_session.refresh(ss)
        assert ss.duration_seconds == pytest.approx(5.5)

    async def test_replace_slides_defaults_duration_when_omitted(
        self, client, db_session
    ):
        # The MCP set_slideshow_slides tool + AI assistant prompt advertise
        # duration_ms as optional (default 7000). A slide that omits it must
        # NOT 400 — it lands on the schema default.
        img = await _seed_image(db_session, filename="nodur.png", is_global=True)
        create = await client.post(
            "/api/assets/slideshow", json={"name": "nodur", "slides": []}
        )
        assert create.status_code == 201
        sid = create.json()["id"]
        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{"source_asset_id": str(img.id)}]},
        )
        assert put.status_code == 200, put.text
        assert put.json()["slide_count"] == 1
        # 7000 ms default → 7.0 s
        assert put.json()["duration_seconds"] == pytest.approx(7.0)
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["duration_ms"] == 7000

    async def test_empty_draft_not_global_allows_non_global_sources(
        self, client, db_session
    ):
        # An admin minting an empty draft (no groups) must NOT lock it global.
        # Otherwise the AI assistant can't then add the owner's non-global
        # sources (the global-source ACL would 400). Mirrors the real
        # assistant create flow: create empty draft → PUT non-global slides.
        create = await client.post(
            "/api/assets/slideshow", json={"name": "owner draft", "slides": []}
        )
        assert create.status_code == 201, create.text
        sid = create.json()["id"]
        ss = await db_session.get(Asset, uuid.UUID(sid))
        assert ss.is_global is False

        non_global = await _seed_image(
            db_session, filename="mine.png", is_global=False
        )
        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{"source_asset_id": str(non_global.id)}]},
        )
        assert put.status_code == 200, put.text
        assert put.json()["slide_count"] == 1


@pytest.mark.asyncio
class TestSlideTransitions:
    """Phase 1a of agora#226: per-slide transition + transition_ms."""

    async def test_create_defaults_transition_to_cut_600(self, client, db_session):
        img = await _seed_image(db_session, filename="t1.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "td",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["transition"] == "cut"
        assert slides[0]["transition_ms"] == 600

    async def test_create_round_trips_explicit_transition(self, client, db_session):
        img = await _seed_image(db_session, filename="t2.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "te",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 2000,
                    "transition": "fade",
                    "transition_ms": 800,
                }],
            },
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["transition"] == "fade"
        assert slides[0]["transition_ms"] == 800

    async def test_replace_round_trips_explicit_transition(self, client, db_session):
        img = await _seed_image(db_session, filename="t3.png", is_global=True)
        sid = (await client.post(
            "/api/assets/slideshow",
            json={
                "name": "tr",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )).json()["id"]
        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{
                "source_asset_id": str(img.id),
                "duration_ms": 1000,
                "transition": "dissolve",
                "transition_ms": 1500,
            }]},
        )
        assert put.status_code == 200, put.text
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["transition"] == "dissolve"
        assert slides[0]["transition_ms"] == 1500

    async def test_rejects_unknown_transition(self, client, db_session):
        img = await _seed_image(db_session, filename="t4.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "tx",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 1000,
                    "transition": "warp_speed",
                }],
            },
        )
        assert resp.status_code in (400, 422), resp.text

    @pytest.mark.parametrize("tx", ["fade_black", "push", "zoom"])
    async def test_new_transition_ids_round_trip(self, client, db_session, tx):
        """The transition set expanded in 0029 — make sure each new ID
        passes Pydantic validation, the DB CHECK constraint, and round-trips
        through GET /slides."""
        img = await _seed_image(db_session, filename=f"t-{tx}.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": f"slideshow-{tx}",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 1000,
                    "transition": tx,
                    "transition_ms": 500,
                }],
            },
        )
        assert resp.status_code in (200, 201), resp.text
        ss_id = resp.json()["id"]
        slides = (await client.get(f"/api/assets/{ss_id}/slides")).json()["slides"]
        assert slides[0]["transition"] == tx
        assert slides[0]["transition_ms"] == 500


    async def test_rejects_transition_ms_above_cap(self, client, db_session):
        img = await _seed_image(db_session, filename="t5.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "th",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 1000,
                    "transition": "fade",
                    "transition_ms": 99999,
                }],
            },
        )
        assert resp.status_code in (400, 422), resp.text

    async def test_transition_change_flips_manifest_content_hash(
        self, client, db_session
    ):
        """Editing the transition is a user-visible content change → the
        structural manifest content hash MUST flip so the device refetches.
        Documented in plan.md Phase 1a (deliberate refetch-storm decision).
        """
        img = await _seed_image(db_session, filename="t6.png", is_global=True)
        sid = (await client.post(
            "/api/assets/slideshow",
            json={
                "name": "th2",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 1000,
                    "transition": "cut",
                    "transition_ms": 600,
                }],
            },
        )).json()["id"]
        ss = await db_session.get(Asset, uuid.UUID(sid))
        await db_session.refresh(ss)
        original_hash = ss.checksum

        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{
                "source_asset_id": str(img.id),
                "duration_ms": 1000,
                "transition": "fade",  # only this changed
                "transition_ms": 600,
            }]},
        )
        assert put.status_code == 200, put.text
        await db_session.refresh(ss)
        assert ss.checksum != original_hash, (
            "transition edit must flip the manifest content hash so devices "
            "refetch on the next sync"
        )

    async def test_transition_ms_change_flips_manifest_content_hash(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="t7.png", is_global=True)
        sid = (await client.post(
            "/api/assets/slideshow",
            json={
                "name": "th3",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 1000,
                    "transition": "fade",
                    "transition_ms": 600,
                }],
            },
        )).json()["id"]
        ss = await db_session.get(Asset, uuid.UUID(sid))
        await db_session.refresh(ss)
        original_hash = ss.checksum

        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{
                "source_asset_id": str(img.id),
                "duration_ms": 1000,
                "transition": "fade",
                "transition_ms": 1200,  # only this changed
            }]},
        )
        assert put.status_code == 200, put.text
        await db_session.refresh(ss)
        assert ss.checksum != original_hash


# ── Per-slide fit / effect (agora#7xx) ──


@pytest.mark.asyncio
class TestSlideFitEffect:
    """Regression for the save -> reopen persistence bug: per-slide
    ``fit`` and ``effect`` must round-trip through GET /slides. They were
    persisted correctly but dropped from the serialized load payload, so
    the editor silently reverted them to defaults (cover/none) on reload.
    """

    async def test_create_defaults_fit_cover_effect_none(self, client, db_session):
        img = await _seed_image(db_session, filename="fx0.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "fx0",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["fit"] == "cover"
        assert slides[0]["effect"] == "none"

    async def test_create_round_trips_explicit_fit_and_effect(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="fx1.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "fx1",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 2000,
                    "fit": "contain",
                    "effect": "ken_burns",
                }],
            },
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["fit"] == "contain"
        assert slides[0]["effect"] == "ken_burns"

    async def test_replace_round_trips_explicit_fit_and_effect(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="fx2.png", is_global=True)
        sid = (await client.post(
            "/api/assets/slideshow",
            json={
                "name": "fx2",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )).json()["id"]
        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{
                "source_asset_id": str(img.id),
                "duration_ms": 1000,
                "fit": "contain",
                "effect": "ken_burns",
            }]},
        )
        assert put.status_code == 200, put.text
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["fit"] == "contain"
        assert slides[0]["effect"] == "ken_burns"

    async def test_create_round_trips_contain_blur_fit(self, client, db_session):
        img = await _seed_image(db_session, filename="fxblur.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "fxblur",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 2000,
                    "fit": "contain_blur",
                }],
            },
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["fit"] == "contain_blur"

    async def test_rejects_unknown_fit(self, client, db_session):
        img = await _seed_image(db_session, filename="fx3.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "fx3",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 1000,
                    "fit": "squish",
                }],
            },
        )
        assert resp.status_code in (400, 422), resp.text

    async def test_rejects_unknown_effect(self, client, db_session):
        img = await _seed_image(db_session, filename="fx4.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "fx4",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 1000,
                    "effect": "explode",
                }],
            },
        )
        assert resp.status_code in (400, 422), resp.text

    # ── Ken Burns effect_direction (agora#261) ──
    # Regression for the HTTP 500 on save: the Ken Burns authoring UI emits
    # the full direction grammar (ZOOM in/out + optional 8-way PAN incl.
    # diagonals, e.g. ``out_up_right``), and the Pydantic schema allows it,
    # but the DB CHECK constraint from migration 0043 only permitted the
    # original six tokens.  Saving any diagonal/zoom+pan direction passed
    # validation then violated the constraint on INSERT/UPDATE → 500.
    # These go through the real DB, so they exercise the CHECK constraint
    # widened in migration 0044.

    @pytest.mark.parametrize(
        "direction",
        [
            "in",
            "out",
            "out_up_right",  # the authoring default for a new ken_burns slide
            "in_down_left",
            "out_left",
            "up_right",  # legacy bare-pan alias
            "down",
        ],
    )
    async def test_create_round_trips_effect_direction(
        self, client, db_session, direction
    ):
        img = await _seed_image(
            db_session, filename=f"kb_{direction}.png", is_global=True
        )
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": f"kb-{direction}",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 2000,
                    "effect": "ken_burns",
                    "effect_direction": direction,
                }],
            },
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["effect"] == "ken_burns"
        assert slides[0]["effect_direction"] == direction

    async def test_replace_round_trips_diagonal_effect_direction(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="kbrepl.png", is_global=True)
        sid = (await client.post(
            "/api/assets/slideshow",
            json={
                "name": "kbrepl",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )).json()["id"]
        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={"slides": [{
                "source_asset_id": str(img.id),
                "duration_ms": 1000,
                "effect": "ken_burns",
                "effect_direction": "out_down_right",
            }]},
        )
        assert put.status_code == 200, put.text
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["effect_direction"] == "out_down_right"

    async def test_create_defaults_effect_direction_in(self, client, db_session):
        img = await _seed_image(db_session, filename="kbdef.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "kbdef",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 1000,
                    "effect": "ken_burns",
                }],
            },
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["effect_direction"] == "in"

    async def test_rejects_unknown_effect_direction(self, client, db_session):
        img = await _seed_image(db_session, filename="kbbad.png", is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "kbbad",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 1000,
                    "effect": "ken_burns",
                    "effect_direction": "diagonal",
                }],
            },
        )
        assert resp.status_code in (400, 422), resp.text

    @pytest.mark.parametrize(
        "loose,canonical",
        [
            ("out-right-down", "out_down_right"),  # the user's MCP case
            ("zoom out right down", "out_down_right"),
            ("right_down", "down_right"),
            ("ZOOM IN UP LEFT", "in_up_left"),
        ],
    )
    async def test_create_normalizes_loose_effect_direction(
        self, client, db_session, loose, canonical
    ):
        img = await _seed_image(
            db_session, filename=f"kbn_{canonical}.png", is_global=True
        )
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": f"kbn-{canonical}",
                "slides": [{
                    "source_asset_id": str(img.id),
                    "duration_ms": 2000,
                    "effect": "ken_burns",
                    "effect_direction": loose,
                }],
            },
        )
        assert resp.status_code == 201, resp.text
        sid = resp.json()["id"]
        slides = (await client.get(f"/api/assets/{sid}/slides")).json()["slides"]
        assert slides[0]["effect_direction"] == canonical


# ── Source-delete guard ──


@pytest.mark.asyncio
class TestSourceDeleteGuard:

    async def test_blocks_delete_when_referenced_by_active_slideshow(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="locked.png", is_global=True)
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "blockit",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert create.status_code == 201
        resp = await client.delete(f"/api/assets/{img.id}")
        assert resp.status_code == 409
        assert "blockit" in resp.json()["detail"]
        assert "active slideshow" in resp.json()["detail"].lower()

    async def test_blocks_delete_when_referenced_by_soft_deleted_slideshow(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="locked2.png", is_global=True)
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "softdeleted",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        sid = uuid.UUID(create.json()["id"])
        # Soft-delete the slideshow (via API, which sets deleted_at)
        del_ss = await client.delete(f"/api/assets/{sid}")
        assert del_ss.status_code == 200, del_ss.text
        # Source delete should still be blocked while the soft-deleted
        # slideshow exists (FK is RESTRICT — reaper will eventually clear).
        resp = await client.delete(f"/api/assets/{img.id}")
        assert resp.status_code == 409
        assert "soft-deleted" in resp.json()["detail"].lower()


# ── Source-side ACL guards ──


@pytest.mark.asyncio
class TestSourceSideACLGuards:

    async def test_unshare_blocked_when_slideshow_in_same_group(
        self, client, db_session
    ):
        g = await _seed_group(db_session, "g3")
        img = await _seed_image(db_session, filename="shared.png", is_global=False)
        await _share(db_session, img, g)
        ss_resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "share-blocker",
                "group_ids": [str(g.id)],
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert ss_resp.status_code == 201, ss_resp.text
        unshare = await client.delete(
            f"/api/assets/{img.id}/share?group_id={g.id}"
        )
        assert unshare.status_code == 409
        assert "share-blocker" in unshare.json()["detail"]

    async def test_unmark_global_blocked_when_global_slideshow_uses_it(
        self, client, db_session
    ):
        img = await _seed_image(db_session, filename="g-src.png", is_global=True)
        ss_resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "global-blocker",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert ss_resp.status_code == 201
        # Toggling off global on the source must be refused
        toggle = await client.post(f"/api/assets/{img.id}/global")
        assert toggle.status_code == 409
        assert "global-blocker" in toggle.json()["detail"]
        # Source is still global
        await db_session.refresh(img)
        assert img.is_global is True


# ── Slideshow-side audience-expansion guards (commit 2) ──


@pytest.mark.asyncio
class TestSlideshowAudienceExpansion:

    async def test_share_slideshow_with_new_group_blocked_when_source_uncovered(
        self, client, db_session
    ):
        g1 = await _seed_group(db_session, "g-cov-1")
        g2 = await _seed_group(db_session, "g-cov-2")
        img = await _seed_image(db_session, filename="src.png", is_global=False)
        await _share(db_session, img, g1)  # source covers g1 only
        ss = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "ssA",
                "group_ids": [str(g1.id)],
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert ss.status_code == 201, ss.text
        sid = ss.json()["id"]

        # Sharing slideshow with g2 must be refused — source doesn't cover g2.
        share = await client.post(f"/api/assets/{sid}/share?group_id={g2.id}")
        assert share.status_code == 409
        assert "src.png" in share.json()["detail"]

        # Slideshow's group set is unchanged
        existing = (await db_session.execute(
            select(GroupAsset.group_id).where(GroupAsset.asset_id == uuid.UUID(sid))
        )).scalars().all()
        assert set(existing) == {g1.id}

    async def test_share_slideshow_with_new_group_allowed_when_source_covers(
        self, client, db_session
    ):
        g1 = await _seed_group(db_session, "g-ok-1")
        g2 = await _seed_group(db_session, "g-ok-2")
        img = await _seed_image(db_session, filename="okay.png", is_global=False)
        await _share(db_session, img, g1)
        await _share(db_session, img, g2)  # source covers both
        ss = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "ssOK",
                "group_ids": [str(g1.id)],
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert ss.status_code == 201, ss.text
        sid = ss.json()["id"]
        share = await client.post(f"/api/assets/{sid}/share?group_id={g2.id}")
        assert share.status_code == 200, share.text

    async def test_mark_slideshow_global_blocked_when_sources_not_global(
        self, client, db_session
    ):
        g = await _seed_group(db_session, "g-mark")
        img = await _seed_image(db_session, filename="ng.png", is_global=False)
        await _share(db_session, img, g)
        ss = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "ssNG",
                "group_ids": [str(g.id)],
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert ss.status_code == 201
        sid = ss.json()["id"]
        # The slideshow is currently non-global. Toggling it global must be
        # refused because the source isn't global.
        toggle = await client.post(f"/api/assets/{sid}/global")
        assert toggle.status_code == 409
        assert "ng.png" in toggle.json()["detail"]
        ss_row = await db_session.get(Asset, uuid.UUID(sid))
        await db_session.refresh(ss_row)
        assert ss_row.is_global is False

    async def test_mark_slideshow_global_allowed_when_sources_are_global(
        self, client, db_session
    ):
        # Create a slideshow with a single global source via a non-admin
        # ish path: scoped user creates inside their group.  Easiest here:
        # admin-no-group-create produces global slideshow already, so to
        # exercise the false→true toggle we first toggle it OFF.
        img = await _seed_image(db_session, filename="g.png", is_global=True)
        ss = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "ssG",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        sid = ss.json()["id"]
        ss_row = await db_session.get(Asset, uuid.UUID(sid))
        # The just-created slideshow is global (admin-no-group rule). Drop
        # global so we can toggle back on.
        ss_row.is_global = False
        await db_session.commit()

        toggle = await client.post(f"/api/assets/{sid}/global")
        assert toggle.status_code == 200, toggle.text
        assert toggle.json()["is_global"] is True


# ── Audit logging ──


@pytest.mark.asyncio
class TestSlideshowAudit:

    async def test_create_writes_audit_log(self, client, db_session):
        img = await _seed_image(db_session, is_global=True)
        resp = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "audited",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        assert resp.status_code == 201
        sid = resp.json()["id"]
        rows = (await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "asset.create_slideshow",
                AuditLog.resource_id == sid,
            )
        )).scalars().all()
        assert len(rows) == 1
        assert "audited" in rows[0].description

    async def test_replace_writes_audit_log(self, client, db_session):
        img = await _seed_image(db_session, filename="x1.png", is_global=True)
        img2 = await _seed_image(db_session, filename="x2.png", is_global=True)
        create = await client.post(
            "/api/assets/slideshow",
            json={
                "name": "replaceaudit",
                "slides": [{"source_asset_id": str(img.id), "duration_ms": 1000}],
            },
        )
        sid = create.json()["id"]
        put = await client.put(
            f"/api/assets/{sid}/slides",
            json={
                "slides": [
                    {"source_asset_id": str(img2.id), "duration_ms": 1500}
                ]
            },
        )
        assert put.status_code == 200
        rows = (await db_session.execute(
            select(AuditLog).where(
                AuditLog.action == "asset.replace_slides",
                AuditLog.resource_id == sid,
            )
        )).scalars().all()
        assert len(rows) == 1
