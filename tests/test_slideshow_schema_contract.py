"""Slideshow manifest schema contract tests.

Pins the wire format so that future minor bumps to the slideshow schema
(``manifest_schema_version`` 1.0 → 1.1 → …) remain backward-compatible
with the v1.0 device parser.  See ``tests/contract/slideshow_v1_0_schema.py``
for the vendored v1.0 shape; see ``plan.md`` Phase 0 for why this gate
exists.

The contract:

* CMS may add NEW optional fields to ``FetchAssetMessage`` (slideshow
  variant) or to ``SlideDescriptor``.
* A v1.0 device parser MUST still be able to deserialize the payload
  — it just ignores fields it doesn't know.

If a test in this file fails after a schema change, the change is not
actually additive and the slideshow schema major should be bumped (gated
via a new capability), not the minor.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from cms.schemas.protocol import (
    FetchAssetMessage,
    MessageType,
    SlideDescriptor,
    SLIDESHOW_MANIFEST_SCHEMA_VERSION_DEFAULT,
    SLIDESHOW_MANIFEST_SCHEMA_VERSION_LATEST,
)
from tests.contract.slideshow_v1_0_schema import (
    FetchAssetMessageV10,
    SlideDescriptorV10,
)


def _build_cms_slideshow_msg(**overrides: Any) -> FetchAssetMessage:
    """Build a representative CMS-emitted slideshow FetchAssetMessage."""
    defaults: dict[str, Any] = {
        "asset_name": "Lobby Slideshow.slideshow",
        "download_url": "",
        "checksum": "deadbeef" * 8,
        "size_bytes": 0,
        "asset_type": "slideshow",
        "slides": [
            SlideDescriptor(
                asset_name="intro.png",
                asset_type="image",
                download_url="/assets/intro.png",
                checksum="a" * 64,
                size_bytes=2048,
                duration_ms=5000,
                play_to_end=False,
            ),
            SlideDescriptor(
                asset_name="promo.mp4",
                asset_type="video",
                download_url="/assets/promo.mp4",
                checksum="b" * 64,
                size_bytes=1_000_000,
                duration_ms=10000,
                play_to_end=True,
            ),
        ],
    }
    defaults.update(overrides)
    return FetchAssetMessage(**defaults)


class TestSlideshowV10Contract:
    """v1.0 device parser must still decode current CMS-emitted payloads."""

    def test_current_cms_payload_decodes_under_v10(self):
        """The baseline contract: a CMS-emitted slideshow message decodes
        cleanly under the vendored v1.0 schema, even when new optional
        fields are present.
        """
        cms_msg = _build_cms_slideshow_msg()

        wire = json.loads(cms_msg.model_dump_json())

        v10 = FetchAssetMessageV10.model_validate(wire)
        assert v10.asset_name == cms_msg.asset_name
        assert v10.asset_type == "slideshow"
        assert v10.slides is not None
        assert len(v10.slides) == 2
        assert v10.slides[0].asset_name == "intro.png"
        assert v10.slides[0].duration_ms == 5000
        assert v10.slides[1].play_to_end is True

    def test_forward_envelope_field_is_ignored(self):
        """Adding a new sibling field on FetchAssetMessage doesn't break
        v1.0.  Phase 1b will populate manifest_schema_version="1.1" on
        the wire; a v1.0 device must ignore it.
        """
        cms_msg = _build_cms_slideshow_msg(manifest_schema_version="1.1")

        wire = json.loads(cms_msg.model_dump_json())
        assert wire.get("manifest_schema_version") == "1.1"

        v10 = FetchAssetMessageV10.model_validate(wire)
        # v1.0 parser doesn't surface the field, but doesn't error either.
        assert not hasattr(v10, "manifest_schema_version") or \
            getattr(v10, "manifest_schema_version", None) is None
        assert v10.slides is not None
        assert len(v10.slides) == 2

    def test_forward_per_slide_field_is_ignored(self):
        """Per-slide additions (transition/transition_ms, Phase 1a) must
        be silently dropped by a v1.0 parser.
        """
        cms_msg = _build_cms_slideshow_msg(
            slides=[
                SlideDescriptor(
                    asset_name="intro.png",
                    asset_type="image",
                    download_url="/assets/intro.png",
                    checksum="a" * 64,
                    size_bytes=2048,
                    duration_ms=5000,
                    play_to_end=False,
                    transition="fade",
                    transition_ms=800,
                ),
            ]
        )
        wire = json.loads(cms_msg.model_dump_json())
        assert wire["slides"][0]["transition"] == "fade"
        assert wire["slides"][0]["transition_ms"] == 800

        v10 = FetchAssetMessageV10.model_validate(wire)
        assert v10.slides is not None
        # Known fields preserved; unknown ones dropped.
        assert v10.slides[0].duration_ms == 5000
        for slide in v10.slides:
            assert not hasattr(slide, "transition") or \
                getattr(slide, "transition", None) is None

    def test_missing_manifest_schema_version_implies_v1_0(self):
        """A payload without the field is, by convention, v1.0.

        This isn't enforced by the schema (the field is optional and
        defaults to None) — it's a documented invariant.  The constant
        in cms.schemas.protocol pins the default.
        """
        assert SLIDESHOW_MANIFEST_SCHEMA_VERSION_DEFAULT == "1.0"
        # Phase 1b bumped LATEST to "1.1" (wall-clock fields); Phase 5
        # composed-in-slideshow bumped it to "1.2" (composed slide
        # descriptors + per-slide siblings); per-slide fit/effect bumps
        # it to "1.3"; per-slide effect_direction + deck shuffle bumps
        # it to "1.4"; per-slide visibility windows (device-evaluated,
        # capability slideshow_visibility_v1) bumps it to "1.5";
        # per-slide video clipping (clip_start_ms, capability
        # slideshow_clip_v1) bumps it to "1.6".  DEFAULT
        # stays at "1.0" — that's the "no version on the wire" fallback.
        assert SLIDESHOW_MANIFEST_SCHEMA_VERSION_LATEST == "1.6"

    def test_forward_wall_clock_fields_are_ignored(self):
        """Phase 1b adds ``cycle_duration_ms`` and ``started_at`` to
        ``FetchAssetMessage``.  A v1.0 device parser must silently drop
        them — same forward-compat invariant as Phase 0/1a fields.
        """
        cms_msg = _build_cms_slideshow_msg(
            manifest_schema_version="1.1",
            cycle_duration_ms=15000,
            started_at="2026-05-23T19:39:45.000Z",
        )
        wire = json.loads(cms_msg.model_dump_json())
        assert wire["cycle_duration_ms"] == 15000
        assert wire["started_at"] == "2026-05-23T19:39:45.000Z"

        v10 = FetchAssetMessageV10.model_validate(wire)
        # v1.0 parser drops the new fields without error.
        assert not hasattr(v10, "cycle_duration_ms") or \
            getattr(v10, "cycle_duration_ms", None) is None
        assert not hasattr(v10, "started_at") or \
            getattr(v10, "started_at", None) is None
        assert v10.slides is not None
        assert len(v10.slides) == 2


class TestSlideshowSchemaFieldDefaults:
    """The new field must serialize cleanly when None (Phase 0 emit shape)."""

    def test_field_omitted_when_none(self):
        """Phase 0 doesn't yet emit ``manifest_schema_version`` — the
        field is None and should be omitted from the JSON unless a
        future phase explicitly populates it.

        We use ``exclude_none=True`` here because that's what the actual
        send path uses; if the round-trip default changes, this guards
        the wire shape.
        """
        cms_msg = _build_cms_slideshow_msg()
        assert cms_msg.manifest_schema_version is None

        wire = json.loads(cms_msg.model_dump_json(exclude_none=True))
        assert "manifest_schema_version" not in wire

    def test_field_serializes_when_set(self):
        cms_msg = _build_cms_slideshow_msg(manifest_schema_version="1.1")
        wire = json.loads(cms_msg.model_dump_json())
        assert wire["manifest_schema_version"] == "1.1"


class TestSlideDescriptorTransitionAllowList:
    """Regression: SlideDescriptor must accept every transition ID listed
    in cms.schemas.asset.SLIDE_TRANSITIONS.  These two lists drifted once
    (the Pydantic validator hard-coded ``cut/fade/dissolve/wipe`` while
    SLIDE_TRANSITIONS grew to include fade_black/push/zoom), which made
    the slideshow fetch path 500 for any slide using the new modes and
    devices stuck in a fetch-retry loop.
    """

    @pytest.mark.parametrize("transition", list(__import__("cms.schemas.asset", fromlist=["SLIDE_TRANSITIONS"]).SLIDE_TRANSITIONS))
    def test_every_known_transition_accepted(self, transition: str) -> None:
        from cms.schemas.protocol import SlideDescriptor

        SlideDescriptor(
            asset_name="slide.png",
            asset_type="image",
            download_url="/x",
            checksum="a" * 64,
            size_bytes=1024,
            duration_ms=2000,
            transition=transition,
            transition_ms=600,
        )

    def test_unknown_transition_rejected(self) -> None:
        from cms.schemas.protocol import SlideDescriptor

        with pytest.raises(ValueError, match="transition must be one of"):
            SlideDescriptor(
                asset_name="slide.png",
                asset_type="image",
                download_url="/x",
                checksum="a" * 64,
                size_bytes=1024,
                duration_ms=2000,
                transition="kaleidoscope",
                transition_ms=600,
            )


class TestSlideEffectDirectionAllowList:
    """Schema 1.4: per-slide ``effect_direction`` (Ken Burns pan/zoom
    direction).  The descriptor must accept every direction listed in
    ``cms.schemas.asset.KEN_BURNS_DIRECTIONS`` and reject unknown ones,
    mirroring the transition allow-list guard above.
    """

    @pytest.mark.parametrize(
        "direction",
        list(__import__("cms.schemas.asset", fromlist=["KEN_BURNS_DIRECTIONS"]).KEN_BURNS_DIRECTIONS),
    )
    def test_every_known_direction_accepted(self, direction: str) -> None:
        sd = SlideDescriptor(
            asset_name="slide.png",
            asset_type="image",
            download_url="/x",
            checksum="a" * 64,
            size_bytes=1024,
            duration_ms=2000,
            effect="ken_burns",
            effect_direction=direction,
        )
        assert sd.effect_direction == direction

    def test_unknown_direction_rejected(self) -> None:
        with pytest.raises(ValueError):
            SlideDescriptor(
                asset_name="slide.png",
                asset_type="image",
                download_url="/x",
                checksum="a" * 64,
                size_bytes=1024,
                duration_ms=2000,
                effect="ken_burns",
                effect_direction="diagonal",
            )

    def test_effect_direction_defaults_to_in(self) -> None:
        """An all-default slide serializes ``effect_direction="in"``, exactly
        like the sibling ``fit``/``effect`` defaults (non-Optional scalars
        that always appear on the wire since manifest 1.3).  Default ``in``
        reproduces the pre-1.4 zoom-in keyframes byte-for-byte.
        """
        sd = SlideDescriptor(
            asset_name="slide.png",
            asset_type="image",
            download_url="/x",
            checksum="a" * 64,
            size_bytes=1024,
            duration_ms=2000,
        )
        assert sd.effect_direction == "in"
        wire = json.loads(sd.model_dump_json(exclude_none=True))
        assert wire["effect_direction"] == "in"

    def test_v10_parser_ignores_effect_direction(self) -> None:
        """A v1.0 device parser must silently drop ``effect_direction``."""
        sd = SlideDescriptor(
            asset_name="slide.png",
            asset_type="image",
            download_url="/x",
            checksum="a" * 64,
            size_bytes=1024,
            duration_ms=2000,
            effect="ken_burns",
            effect_direction="left",
        )
        wire = json.loads(sd.model_dump_json())
        assert wire["effect_direction"] == "left"
        v10 = SlideDescriptorV10.model_validate(wire)
        assert getattr(v10, "effect_direction", None) is None


class TestSlideshowShuffleEnvelope:
    """Schema 1.4: deck-level ``shuffle`` + stable ``shuffle_seed`` on the
    slideshow ``FetchAssetMessage``.  Additive + forward-compatible.
    """

    def test_shuffle_fields_serialize_when_set(self) -> None:
        msg = _build_cms_slideshow_msg(shuffle=True, shuffle_seed=12345)
        wire = json.loads(msg.model_dump_json())
        assert wire["shuffle"] is True
        assert wire["shuffle_seed"] == 12345

    def test_shuffle_fields_omitted_when_none(self) -> None:
        msg = _build_cms_slideshow_msg()
        wire = json.loads(msg.model_dump_json(exclude_none=True))
        assert "shuffle" not in wire
        assert "shuffle_seed" not in wire

    def test_v10_parser_ignores_shuffle_fields(self) -> None:
        msg = _build_cms_slideshow_msg(shuffle=True, shuffle_seed=777)
        wire = json.loads(msg.model_dump_json())
        v10 = FetchAssetMessageV10.model_validate(wire)
        assert getattr(v10, "shuffle", None) is None
        assert getattr(v10, "shuffle_seed", None) is None
        assert v10.slides is not None and len(v10.slides) == 2
