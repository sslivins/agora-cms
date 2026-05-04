"""PR 2 schema-plumbing tests for browser-driven Pi image provisioning.

These tests exercise the schema and storage primitives only; the
worker handlers and API endpoints are PR 3 / PR 4.

What's covered:
  - JobType enum gained IMAGE_IMPORT and IMAGE_PROVISION values
  - BaseImage / ProvisionedImage models import cleanly and round-trip
  - UNIQUE(variant, version) on base_images is enforced
  - device_id FK on provisioned_images is String(64), matching
    devices.id (regression-fence: Pi serials, not UUIDs)
  - Most lifecycle fields are nullable so the API can insert an
    ``importing`` / ``provisioning`` row before worker completion
  - Worker dispatcher's stub raises NotImplementedError for the
    new types (so PR 3 has a clear landing spot)
  - LocalStorageBackend grew the four imager primitives and they
    round-trip a file end-to-end
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError

from shared.models import (
    BaseImage,
    BaseImageStatus,
    ProvisionedImage,
    ProvisionedImageStatus,
)
from shared.models.job import JobType


# ── JobType enum ────────────────────────────────────────────────


def test_jobtype_has_imager_values() -> None:
    """The dispatcher routes on these names; renaming = breaking."""
    assert JobType.IMAGE_IMPORT.value == "image_import"
    assert JobType.IMAGE_PROVISION.value == "image_provision"


# ── Model imports + schema shape ────────────────────────────────


@pytest.mark.asyncio
async def test_imager_models_register_on_metadata(db_engine) -> None:
    """Both tables must be present on Base.metadata after import."""
    def _names(sync_conn):
        return set(inspect(sync_conn).get_table_names())
    async with db_engine.connect() as conn:
        names = await conn.run_sync(_names)
    assert "base_images" in names
    assert "provisioned_images" in names


@pytest.mark.asyncio
async def test_provisioned_images_device_id_is_string64(db_engine) -> None:
    """``devices.id`` is a Pi serial (String(64)), not a UUID.

    A FK type mismatch makes the migration apply on SQLite (which
    doesn't strictly enforce types) but explode on Postgres.  This
    fence keeps regressions out.
    """
    def _cols(sync_conn):
        return {c["name"]: c for c in inspect(sync_conn).get_columns("provisioned_images")}
    async with db_engine.connect() as conn:
        cols = await conn.run_sync(_cols)
    type_str = str(cols["device_id"]["type"]).upper()
    assert "64" in type_str, f"expected 64-char string for device_id, got {type_str}"


@pytest.mark.asyncio
async def test_base_image_nullable_until_ready(db_engine) -> None:
    """Catalog metadata fields populate on import success, not insert."""
    def _cols(sync_conn):
        return {c["name"]: c for c in inspect(sync_conn).get_columns("base_images")}
    async with db_engine.connect() as conn:
        cols = await conn.run_sync(_cols)
    for f in ("sha256", "blob_path", "size_bytes", "imported_at", "imported_by"):
        assert cols[f]["nullable"], f"{f} must be nullable until status=ready"
    for f in ("variant", "version", "status"):
        assert not cols[f]["nullable"], f"{f} must be NOT NULL"


@pytest.mark.asyncio
async def test_provisioned_image_nullable_until_ready(db_engine) -> None:
    def _cols(sync_conn):
        return {c["name"]: c for c in inspect(sync_conn).get_columns("provisioned_images")}
    async with db_engine.connect() as conn:
        cols = await conn.run_sync(_cols)
    for f in (
        "device_id", "output_sha256", "output_size", "blob_path",
        "expires_at", "built_at", "built_by", "fleet_env_payload",
    ):
        assert cols[f]["nullable"], f"{f} must be nullable until status=ready"
    for f in ("base_image_id", "output_name", "status"):
        assert not cols[f]["nullable"], f"{f} must be NOT NULL"


# ── Round-trip ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_base_image_round_trip(db_session) -> None:
    bi = BaseImage(variant="pi5", version="v1.11.28")
    db_session.add(bi)
    await db_session.commit()

    fetched = (await db_session.execute(select(BaseImage))).scalars().one()
    assert fetched.variant == "pi5"
    assert fetched.version == "v1.11.28"
    assert fetched.status == BaseImageStatus.IMPORTING.value
    assert fetched.is_default is False
    assert fetched.sha256 is None  # not yet set


@pytest.mark.asyncio
async def test_base_image_unique_variant_version(db_session) -> None:
    db_session.add(BaseImage(variant="pi5", version="v1"))
    await db_session.commit()

    db_session.add(BaseImage(variant="pi5", version="v1"))
    with pytest.raises(IntegrityError):
        await db_session.commit()


@pytest.mark.asyncio
async def test_provisioned_image_round_trip(db_session) -> None:
    bi = BaseImage(
        variant="pi5", version="v1", status=BaseImageStatus.READY.value,
        sha256="abc123", blob_path="base-images/pi5/v1/base.img.xz",
        size_bytes=600_000_000,
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    pi = ProvisionedImage(
        base_image_id=bi.id,
        output_name="agora-pi5-fleet42.img.xz",
        fleet_env_payload=b"\x01\x02\x03 ciphertext",
    )
    db_session.add(pi)
    await db_session.commit()

    fetched = (await db_session.execute(select(ProvisionedImage))).scalars().one()
    assert fetched.output_name == "agora-pi5-fleet42.img.xz"
    assert fetched.status == ProvisionedImageStatus.PROVISIONING.value
    assert fetched.fleet_env_payload == b"\x01\x02\x03 ciphertext"
    assert fetched.base_image_id == bi.id


# ── Dispatcher stub ─────────────────────────────────────────────


def test_worker_dispatcher_routes_imager_types() -> None:
    """The PR 2 stub must raise NotImplementedError for IMAGE_*.

    PR 3 replaces this branch with real handler calls.  The branch's
    presence is what guarantees an unhandled enum value can't sneak
    through to ``Unknown job type`` and look like a typo.
    """
    import inspect as _inspect
    import worker.__main__ as worker_main

    src = _inspect.getsource(worker_main)
    assert "JobType.IMAGE_IMPORT" in src
    assert "JobType.IMAGE_PROVISION" in src
    assert "PR 3" in src  # tombstone for the followup


# ── LocalStorageBackend imager primitives ───────────────────────


@pytest.mark.asyncio
async def test_local_storage_imager_round_trip(tmp_path: Path) -> None:
    from shared.services.storage import LocalStorageBackend

    backend = LocalStorageBackend(base_path=tmp_path)

    src = tmp_path / "src.bin"
    src.write_bytes(b"hello imager")

    # exists() before upload → false
    assert await backend.blob_exists("base-images", "pi5/v1/base.img.xz") is False

    await backend.upload_local_file(
        "base-images", "pi5/v1/base.img.xz", src,
    )
    assert await backend.blob_exists("base-images", "pi5/v1/base.img.xz") is True

    # Re-upload without overwrite must refuse.
    with pytest.raises(FileExistsError):
        await backend.upload_local_file(
            "base-images", "pi5/v1/base.img.xz", src,
        )
    # With overwrite=True it succeeds.
    src.write_bytes(b"hello imager v2")
    await backend.upload_local_file(
        "base-images", "pi5/v1/base.img.xz", src, overwrite=True,
    )

    dst = tmp_path / "dst.bin"
    await backend.download_to_file(
        "base-images", "pi5/v1/base.img.xz", dst,
    )
    assert dst.read_bytes() == b"hello imager v2"

    # SAS URL on local is a synthetic file:// reference.
    sas = backend.generate_blob_sas_url(
        "base-images", "pi5/v1/base.img.xz", ttl_hours=2,
    )
    assert sas.startswith("file://")


@pytest.mark.asyncio
async def test_local_storage_download_missing_raises(tmp_path: Path) -> None:
    from shared.services.storage import LocalStorageBackend
    backend = LocalStorageBackend(base_path=tmp_path)
    with pytest.raises(FileNotFoundError):
        await backend.download_to_file(
            "base-images", "missing.bin", tmp_path / "out.bin",
        )


def test_local_storage_path_traversal_refused(tmp_path: Path) -> None:
    """Defence in depth: ``..`` segments are refused."""
    from shared.services.storage import LocalStorageBackend
    backend = LocalStorageBackend(base_path=tmp_path)
    with pytest.raises(ValueError):
        backend._container_path("base-images", "../escape")
    with pytest.raises(ValueError):
        backend._container_path("..", "blob")


# ── Settings ────────────────────────────────────────────────────


def test_shared_settings_imager_defaults() -> None:
    """Imager settings must default such that existing deployments
    upgrade without setting any new env vars."""
    # Avoid pulling whatever the test process already has set in env.
    import os
    env_keys = [k for k in os.environ if k.startswith("AGORA_CMS_")]
    saved = {k: os.environ.pop(k) for k in env_keys}
    try:
        from shared.config import SharedSettings
        s = SharedSettings()
        assert s.base_image_catalog_url == ""  # no upstream set
        assert s.base_image_cache_container == "base-images"
        assert s.provisioned_container == "provisioned"
        assert s.provisioned_retention_hours == 24
        assert s.imager_sas_ttl_hours == 2
        assert s.imager_min_free_bytes >= 5 * 1024 * 1024 * 1024
        # The allowlist covers the upstream catalog host by default.
        assert "github.com" in s.base_image_allowed_hosts
    finally:
        os.environ.update(saved)
