# Vendored v1.0 slideshow manifest schema for contract pinning.
#
# Snapshot of the FetchAssetMessage slideshow shape as it existed BEFORE the
# manifest_schema_version field was introduced (see plan.md Phase 0,
# agora#226 wall-clock work).  This file is the contract gate: every future
# minor bump to the slideshow wire format MUST keep a v1.0 device parser
# able to deserialize the CMS-emitted payload by ignoring unknown fields.
#
# tests/test_slideshow_schema_contract.py round-trips current CMS output
# through these models to catch additive-evolution regressions.
#
# If you find yourself needing to MODIFY this file to make tests pass, that
# is a red flag: the new field is probably not actually additive, or the
# v1.0 contract has been broken.  Bump the slideshow schema major (not
# minor) and gate via a new capability string instead.
#
# DO NOT MODIFY MANUALLY.
# ruff: noqa
"""Pinned v1.0 slideshow manifest validator.

Mirrors the pre-versioning shape:

    FetchAssetMessage (slideshow variant):
      - type, asset_name, download_url, checksum, size_bytes
      - asset_type = "slideshow"
      - slides: list[SlideDescriptor]

    SlideDescriptor:
      - asset_name, asset_type ("image"|"video"), download_url
      - checksum, size_bytes, duration_ms, play_to_end (default False)

Both models set ``extra="ignore"`` so a v1.0 device parsing a forward
v1.x payload keeps the fields it knows and silently drops the rest.
That's the whole point of the additive-evolution model.
"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, ConfigDict


class SlideDescriptorV10(BaseModel):
    """v1.0 SlideDescriptor.  Unknown fields ignored (forward compat)."""

    model_config = ConfigDict(extra="ignore")

    asset_name: str
    asset_type: str  # "image" or "video"
    download_url: str
    checksum: str
    size_bytes: int
    duration_ms: int
    play_to_end: bool = False


class FetchAssetMessageV10(BaseModel):
    """v1.0 FetchAssetMessage (slideshow variant).  Unknown fields ignored.

    A v1.0 device sees a v1.x slideshow payload and successfully decodes
    the slide list — any new sibling fields on the envelope
    (``manifest_schema_version``, ``cycle_duration_ms``, ``started_at``)
    or new per-slide fields (``transition``, ``transition_ms``) are
    silently dropped.  The device continues to play the deck via the
    legacy relative-timer chain.
    """

    model_config = ConfigDict(extra="ignore")

    type: str
    asset_name: str
    download_url: str
    checksum: str
    size_bytes: int
    asset_type: Optional[str] = None
    slides: Optional[List[SlideDescriptorV10]] = None
