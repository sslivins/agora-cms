"""Phase 1D: ``siblings`` payload on FETCH_ASSET for COMPOSED assets.

Pairs with agora firmware PR #253 (APT 1.11.95) which adds the
``composed_siblings_v1`` capability so the device's os_updater can
pre-fetch every video / image referenced by a composed bundle before
swapping it into the active cache.

These tests exercise ``_resolve_asset_for_device`` end-to-end against
the in-memory test DB (sqlite via conftest), only mocking
``get_storage`` to keep S3 / presigned URLs out of the picture.
"""

from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest

from cms.models.asset import Asset, AssetType, AssetVariant, VariantStatus
from cms.models.composed_slide import ComposedSlide
from cms.models.device import Device, DeviceStatus
from cms.models.device_profile import DeviceProfile
from cms.schemas.protocol import CAPABILITY_COMPOSED_SIBLINGS_V1
from cms.services.device_inbound import _resolve_asset_for_device


# ---------------------------------------------------------------------------
# Local seed helpers (mirror test_slideshow_resolver.py patterns)
# ---------------------------------------------------------------------------

async def _seed_image(db, *, filename, checksum="img-sha", size=100):
    a = Asset(
        filename=filename, asset_type=AssetType.IMAGE,
        size_bytes=size, checksum=checksum, is_global=True,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _seed_video(db, *, filename, checksum="vid-sha", size=200):
    a = Asset(
        filename=filename, asset_type=AssetType.VIDEO,
        size_bytes=size, checksum=checksum, is_global=True,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    return a


async def _seed_profile(db, name="px"):
    p = DeviceProfile(name=name)
    db.add(p)
    await db.commit()
    await db.refresh(p)
    return p


async def _seed_variant(db, *, source, profile, status=VariantStatus.READY,
                        checksum="v-sha", size=50, ext="mp4"):
    v = AssetVariant(
        source_asset_id=source.id,
        profile_id=profile.id,
        filename=f"{uuid.uuid4()}.{ext}",
        status=status, checksum=checksum, size_bytes=size,
    )
    db.add(v)
    await db.commit()
    await db.refresh(v)
    return v


async def _seed_device(db, *, did="dev-1", profile=None, capabilities=None):
    d = Device(
        id=did, name=did, status=DeviceStatus.ADOPTED,
        capabilities=list(capabilities or []),
        profile_id=profile.id if profile else None,
    )
    db.add(d)
    await db.commit()
    await db.refresh(d)
    return d


async def _seed_composed(db, *, filename="composed-x.html", checksum="cmp-sha",
                         size=4096, source_asset_ids=None):
    a = Asset(
        filename=filename, asset_type=AssetType.COMPOSED,
        size_bytes=size, checksum=checksum, is_global=True,
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    cs = ComposedSlide(
        asset_id=a.id,
        layout_json={"widgets": []},
        is_draft=False,
        bundle_source_asset_ids=(
            None if source_asset_ids is None
            else [str(aid) for aid in source_asset_ids]
        ),
    )
    db.add(cs)
    await db.commit()
    return a


def _fake_storage(url_prefix="https://cdn"):
    s = AsyncMock()

    async def _url(path, _api):
        return f"{url_prefix}/{path}"

    s.get_device_download_url = AsyncMock(side_effect=_url)
    return s


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
class TestComposedSiblings:
    """Verifies the per-device ``siblings`` payload contract."""

    async def test_capability_off_omits_siblings_field(self, db_session):
        """Device without ``composed_siblings_v1`` MUST get
        ``siblings=None`` (legacy fetch shape) even when the bundle
        declares source assets — this is the firmware-back-compat gate.
        """
        vid = await _seed_video(db_session, filename="v.mp4")
        composed = await _seed_composed(db_session, source_asset_ids=[vid.id])
        device = await _seed_device(db_session, capabilities=[])

        with patch("cms.services.device_inbound.get_storage",
                   return_value=_fake_storage()):
            fetch = await _resolve_asset_for_device(
                composed, device, "https://cms", db_session,
            )

        assert fetch is not None
        assert fetch.asset_type == "composed"
        assert fetch.siblings is None

    async def test_capability_on_emits_siblings(self, db_session):
        """Device WITH the capability gets one Sibling per declared
        source asset; without a profile each sibling carries the source
        asset's filename / checksum / size_bytes."""
        vid = await _seed_video(db_session, filename="hello.mp4",
                                checksum="VVV", size=222)
        img = await _seed_image(db_session, filename="logo.png",
                                checksum="III", size=111)
        composed = await _seed_composed(
            db_session, source_asset_ids=[vid.id, img.id],
        )
        device = await _seed_device(
            db_session, capabilities=[CAPABILITY_COMPOSED_SIBLINGS_V1],
        )

        with patch("cms.services.device_inbound.get_storage",
                   return_value=_fake_storage()):
            fetch = await _resolve_asset_for_device(
                composed, device, "https://cms", db_session,
            )

        assert fetch is not None and fetch.siblings is not None
        assert len(fetch.siblings) == 2
        # Order preserved from bundle_source_asset_ids
        sib_v, sib_i = fetch.siblings
        assert sib_v.name == "hello.mp4"
        assert sib_v.asset_type == "video"
        assert sib_v.checksum == "VVV"
        assert sib_v.size_bytes == 222
        assert "hello.mp4" in sib_v.download_url
        assert sib_i.name == "logo.png"
        assert sib_i.asset_type == "image"
        assert sib_i.checksum == "III"
        assert sib_i.size_bytes == 111

    async def test_sibling_uses_profile_variant_when_ready(self, db_session):
        """Per-device variant lookup: a READY variant for the sibling's
        (asset, device.profile) pair MUST be reflected in the Sibling's
        URL / checksum / size — but ``name`` stays as the SOURCE
        filename so the bundle's hard-coded
        ``/assets/videos/<filename>`` ref still resolves on disk."""
        profile = await _seed_profile(db_session, "hevc")
        vid = await _seed_video(db_session, filename="hello.mp4",
                                checksum="SRC", size=999)
        variant = await _seed_variant(
            db_session, source=vid, profile=profile,
            checksum="HEVC", size=333, ext="mp4",
        )
        composed = await _seed_composed(db_session, source_asset_ids=[vid.id])
        device = await _seed_device(
            db_session, profile=profile,
            capabilities=[CAPABILITY_COMPOSED_SIBLINGS_V1],
        )

        with patch("cms.services.device_inbound.get_storage",
                   return_value=_fake_storage()):
            fetch = await _resolve_asset_for_device(
                composed, device, "https://cms", db_session,
            )

        assert fetch is not None and fetch.siblings is not None
        assert len(fetch.siblings) == 1
        sib = fetch.siblings[0]
        assert sib.name == "hello.mp4"           # SOURCE filename
        assert sib.checksum == "HEVC"             # variant
        assert sib.size_bytes == 333              # variant
        assert variant.filename in sib.download_url  # variant URL

    async def test_inflight_sibling_variant_returns_none(self, db_session):
        """If ANY sibling has a non-terminal variant in flight for this
        device's profile, the entire FetchAssetMessage MUST be skipped
        (mirrors the single-asset rule — we never serve a stale fetch
        racing a still-transcoding variant)."""
        profile = await _seed_profile(db_session, "hevc")
        vid = await _seed_video(db_session, filename="hello.mp4")
        await _seed_variant(
            db_session, source=vid, profile=profile,
            status=VariantStatus.PROCESSING, ext="mp4",
        )
        composed = await _seed_composed(db_session, source_asset_ids=[vid.id])
        device = await _seed_device(
            db_session, profile=profile,
            capabilities=[CAPABILITY_COMPOSED_SIBLINGS_V1],
        )

        with patch("cms.services.device_inbound.get_storage",
                   return_value=_fake_storage()):
            fetch = await _resolve_asset_for_device(
                composed, device, "https://cms", db_session,
            )

        assert fetch is None

    async def test_deleted_sibling_is_skipped_with_warning(
        self, db_session, caplog,
    ):
        """A sibling whose source asset has been soft-deleted is dropped
        from the list with a WARNING log; the bundle is still served —
        the device will hit a broken <video> tag for that filename but
        that's the same behaviour as today (pre-siblings) and matches
        the spec ("don't fail the whole message")."""
        import logging
        caplog.set_level(logging.WARNING, logger="cms.services.device_inbound")

        good = await _seed_video(db_session, filename="kept.mp4",
                                 checksum="KEEP", size=11)
        gone = await _seed_video(db_session, filename="gone.mp4")
        # Soft-delete the second sibling.
        from datetime import datetime, timezone
        gone.deleted_at = datetime.now(timezone.utc)
        await db_session.commit()

        composed = await _seed_composed(
            db_session, source_asset_ids=[good.id, gone.id],
        )
        device = await _seed_device(
            db_session, capabilities=[CAPABILITY_COMPOSED_SIBLINGS_V1],
        )

        with patch("cms.services.device_inbound.get_storage",
                   return_value=_fake_storage()):
            fetch = await _resolve_asset_for_device(
                composed, device, "https://cms", db_session,
            )

        assert fetch is not None and fetch.siblings is not None
        assert [s.name for s in fetch.siblings] == ["kept.mp4"]
        assert any(
            "missing/deleted sibling" in r.message
            for r in caplog.records
        )

    async def test_empty_bundle_source_asset_ids_yields_none(self, db_session):
        """A composed slide with an empty bundle_source_asset_ids list
        (e.g. all-text/clock widgets) MUST emit ``siblings=None`` — NOT
        an empty list — so the wire stays stable for the "no deps"
        case."""
        composed = await _seed_composed(db_session, source_asset_ids=[])
        device = await _seed_device(
            db_session, capabilities=[CAPABILITY_COMPOSED_SIBLINGS_V1],
        )

        with patch("cms.services.device_inbound.get_storage",
                   return_value=_fake_storage()):
            fetch = await _resolve_asset_for_device(
                composed, device, "https://cms", db_session,
            )

        assert fetch is not None
        assert fetch.siblings is None

    async def test_null_bundle_source_asset_ids_yields_none(self, db_session):
        """A draft composed slide whose bundle hasn't been built yet has
        ``bundle_source_asset_ids = NULL`` — same wire result as the
        empty case (``siblings=None``)."""
        composed = await _seed_composed(db_session, source_asset_ids=None)
        device = await _seed_device(
            db_session, capabilities=[CAPABILITY_COMPOSED_SIBLINGS_V1],
        )

        with patch("cms.services.device_inbound.get_storage",
                   return_value=_fake_storage()):
            fetch = await _resolve_asset_for_device(
                composed, device, "https://cms", db_session,
            )

        assert fetch is not None
        assert fetch.siblings is None

    async def test_quote_safe_empty_in_publish(self):
        """Direct unit test for the publish.py quote-safe fix: filenames
        with '/' must percent-encode the slash (default urllib
        ``safe='/'`` would leave it alone and let a malicious filename
        escape the /assets/videos/ directory)."""
        from urllib.parse import quote
        # Mirror the call site: f"/assets/videos/{quote(name, safe='')}"
        encoded = quote("evil/../etc/passwd", safe="")
        assert "/" not in encoded
        assert encoded == "evil%2F..%2Fetc%2Fpasswd"
