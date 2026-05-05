"""Unit tests for ``shared.services.imager_catalog._normalize_catalog``.

The agora release pipeline (``build-image.yml``) publishes the catalog
as a list-of-objects with a top-level ``version`` key.  Older fixtures
and the imager API/worker code expect a dict-keyed-by-variant shape
with a top-level ``ref``.  We canonicalize both into the dict shape so
all callers can keep using ``variants.get(name)``.
"""

from __future__ import annotations

import pytest

from shared.services.imager_catalog import CatalogError, _normalize_catalog


def test_agora_published_list_shape_normalized_to_dict():
    """Mirrors the real catalog.json published by sslivins/agora."""
    doc = {
        "schemaVersion": 1,
        "generatedAt": "2026-05-04T12:00:00Z",
        "version": "v1.11.34",
        "variants": [
            {
                "variant": "pi5",
                "version": "v1.11.34",
                "filename": "agora-v1.11.34-pi5.img.xz",
                "url": "https://github.com/x/y/releases/download/v1.11.34/agora-v1.11.34-pi5.img.xz",
                "sha256": "abc123",
                "compressedBytes": 1151077188,
                "uncompressedBytes": 4_000_000_000,
            },
            {
                "variant": "pi4",
                "version": "v1.11.34",
                "filename": "agora-v1.11.34-pi4.img.xz",
                "url": "https://github.com/x/y/releases/download/v1.11.34/agora-v1.11.34-pi4.img.xz",
                "sha256": "def456",
                "compressedBytes": 1_000_000_000,
                "uncompressedBytes": 3_500_000_000,
            },
        ],
    }
    out = _normalize_catalog(doc)

    assert out["ref"] == "v1.11.34"
    assert isinstance(out["variants"], dict)
    assert set(out["variants"].keys()) == {"pi5", "pi4"}

    pi5 = out["variants"]["pi5"]
    assert pi5["url"].endswith("pi5.img.xz")
    assert pi5["sha256"] == "abc123"
    # compressedBytes mirrored to size_bytes for CatalogEntryOut.
    assert pi5["size_bytes"] == 1151077188
    assert pi5["compressedBytes"] == 1151077188


def test_legacy_dict_shape_passes_through():
    """Existing fixtures used the dict-keyed shape with ``ref``."""
    doc = {
        "ref": "v1.0.0",
        "variants": {
            "pi5": {
                "url": "https://example.com/a.img.xz",
                "sha256": "a" * 64,
                "size_bytes": 123,
            }
        },
    }
    out = _normalize_catalog(doc)
    assert out["ref"] == "v1.0.0"
    assert out["variants"]["pi5"]["size_bytes"] == 123


def test_missing_ref_falls_back_to_version():
    out = _normalize_catalog({"version": "v9", "variants": []})
    assert out["ref"] == "v9"


def test_existing_ref_wins_over_version():
    out = _normalize_catalog({"ref": "v1", "version": "v9", "variants": []})
    assert out["ref"] == "v1"


def test_list_entries_without_variant_key_are_skipped():
    doc = {
        "version": "v1",
        "variants": [
            {"variant": "pi5", "url": "https://e/x.img.xz", "sha256": "x"},
            {"url": "no-variant-key"},
            "garbage",
        ],
    }
    out = _normalize_catalog(doc)
    assert list(out["variants"].keys()) == ["pi5"]


def test_root_must_be_object():
    with pytest.raises(CatalogError):
        _normalize_catalog([{"variant": "pi5"}])


def test_size_bytes_preserved_when_present():
    """Don't clobber an explicit size_bytes with compressedBytes."""
    doc = {
        "version": "v1",
        "variants": [
            {
                "variant": "pi5",
                "url": "https://e/x.img.xz",
                "sha256": "x",
                "size_bytes": 999,
                "compressedBytes": 111,
            }
        ],
    }
    out = _normalize_catalog(doc)
    assert out["variants"]["pi5"]["size_bytes"] == 999
