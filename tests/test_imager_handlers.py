"""Worker handler tests for browser-driven Pi image provisioning (PR 3).

Covers:

* Import handler happy path with stamped (source_url, expected_sha256).
* Import idempotency (already-READY row returns True without I/O).
* Import terminal failures: disallowed host, redirect to disallowed
  host, SHA256 mismatch, size overflow, malformed catalog entry.
* Import retryable failures: low disk space, missing row.
* Import catalog-fallback path when the row does not have stamped
  URL/SHA (PR 4 will populate at enqueue; PR 3 needs the fallback
  for tests + safety).
* Provision happy path (with mocked ``build_provisioned``).
* Provision idempotency.
* Provision base-not-READY: IMPORTING is retryable, FAILED is terminal.
* Provision tenant blob drift (SHA256 mismatch on cached base).
* Provision invalid UTF-8 payload.
* Provision clears ``fleet_env_payload`` on terminal failure + on
  success, leaves it on retryable failure.
* Provision low disk retryable.

The real ``build_provisioned`` pipeline is tested in PR 1's
``tests/test_imager_pipeline.py``.  We mock it here because (a)
parted/mtools/xz are not portable to Windows test runs and (b) the
handler's job is wiring + status transitions, not subprocess
behavior.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from shared.models.imager import (
    BaseImage,
    BaseImageStatus,
    ProvisionedImage,
    ProvisionedImageStatus,
)
from shared.models.job import Job, JobType
from shared.services.storage import LocalStorageBackend, init_storage
from worker import imager_handlers
from worker.imager_handlers import (
    TerminalImagerError,
    import_base_image_by_id,
    provision_image_by_id,
)


# Default settings sufficient for handler tests; per-test overrides
# happen via ``dataclasses.replace``-style ``SimpleNamespace`` updates.
def _make_settings(tmp_path: Path, **overrides: Any) -> SimpleNamespace:
    scratch_path = tmp_path / "scratch"
    base = dict(
        imager_scratch_path=str(scratch_path),
        resolved_imager_scratch_path=scratch_path,
        imager_min_free_bytes=1,  # tests run in tmpdirs with plenty of room
        base_image_allowed_hosts=(
            "github.com,objects.githubusercontent.com,"
            "release-assets.githubusercontent.com"
        ),
        # PR 7: catalog URL is no longer on Settings.  Tests that
        # exercise the worker fallback path seed a row in
        # ``cms_settings`` via ``set_catalog_url`` instead.
        base_image_cache_container="base-images",
        provisioned_container="provisioned",
        provisioned_retention_hours=24,
        imager_sas_ttl_hours=2,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest_asyncio.fixture
async def session_factory(db_engine):
    """async_sessionmaker matching the worker's signature."""
    return async_sessionmaker(db_engine, expire_on_commit=False)


@pytest_asyncio.fixture
async def storage_backend(tmp_path):
    """LocalStorageBackend rooted at ``tmp_path/blobs`` and registered
    as the global ``shared.services.storage`` backend."""
    backend = LocalStorageBackend(base_path=tmp_path / "blobs")
    init_storage(backend)
    return backend


def _patch_httpx(monkeypatch: pytest.MonkeyPatch, handler) -> None:
    """Swap ``httpx.AsyncClient`` (as imported by ``imager_handlers``)
    for one backed by a ``MockTransport`` running ``handler``."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(imager_handlers.httpx, "AsyncClient", _factory)


# ── Sanity ─────────────────────────────────────────────────────────


def test_terminal_imager_error_is_exception() -> None:
    """Dispatcher catches it explicitly via ``except TerminalImagerError``."""
    assert issubclass(TerminalImagerError, Exception)


def test_scratch_dir_falls_back_to_asset_storage_when_unset(tmp_path: Path) -> None:
    """Default ``imager_scratch_path=None`` must not crash.

    In prod the worker's container app does NOT set IMAGER_SCRATCH_PATH;
    ``shared.config.Settings.imager_scratch_path`` defaults to ``None``.
    The docstring promises a fallback to a subdirectory under
    ``asset_storage_path``; this test pins that contract so we don't
    regress to ``Path(None)`` which TypeErrors the worker.
    """
    settings = SimpleNamespace(
        imager_scratch_path=None,
        asset_storage_path=tmp_path / "assets",
        resolved_imager_scratch_path=tmp_path / "assets" / "imager-scratch",
    )
    target = uuid.uuid4()
    scratch = imager_handlers._scratch_dir(settings, "import", target)

    assert scratch.is_relative_to(tmp_path / "assets")
    assert "imager-scratch" in scratch.parts
    assert f"import-{target}-" in scratch.name


def test_scratch_dir_honors_explicit_imager_scratch_path(tmp_path: Path) -> None:
    """When operators set IMAGER_SCRATCH_PATH, fallback is skipped."""
    explicit = tmp_path / "dedicated-scratch"
    settings = SimpleNamespace(
        imager_scratch_path=explicit,
        asset_storage_path=tmp_path / "assets",
        resolved_imager_scratch_path=explicit,
    )
    target = uuid.uuid4()
    scratch = imager_handlers._scratch_dir(settings, "build", target)

    assert scratch.is_relative_to(explicit)
    assert "imager-scratch" not in scratch.parts


# ── Import handler ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_happy_path_with_stamped_url(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    """Stamped (source_url, expected_sha256) -> direct download, upload,
    row -> READY with sha/size/blob_path populated."""
    payload = b"pretend-this-is-a-real-image-xz-blob" * 32
    expected_sha = hashlib.sha256(payload).hexdigest()
    settings = _make_settings(tmp_path)

    bi = BaseImage(
        variant="pi5", version="v1.11.28",
        source_url=("https://objects.githubusercontent.com/foo/"
                    "agora-pi5.img.xz"),
        expected_sha256=expected_sha,
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "objects.githubusercontent.com"
        return httpx.Response(200, content=payload)

    _patch_httpx(monkeypatch, handle)

    ok = await import_base_image_by_id(session_factory, settings, bi.id)
    assert ok is True

    async with session_factory() as db:
        fresh = (await db.execute(
            select(BaseImage).where(BaseImage.id == bi.id)
        )).scalar_one()
    assert fresh.status == BaseImageStatus.READY.value
    assert fresh.sha256 == expected_sha
    assert fresh.size_bytes == len(payload)
    assert fresh.blob_path == "pi5/v1.11.28/base.img.xz"
    assert fresh.imported_at is not None

    # Blob is in tenant container.
    assert await storage_backend.blob_exists(
        "base-images", "pi5/v1.11.28/base.img.xz"
    )


@pytest.mark.asyncio
async def test_import_idempotent_when_already_ready(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    """READY row -> True, no HTTP, no blob writes."""
    settings = _make_settings(tmp_path)
    bi = BaseImage(
        variant="pi5", version="v1",
        status=BaseImageStatus.READY.value,
        sha256="a" * 64, blob_path="pi5/v1/base.img.xz", size_bytes=100,
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    def fail(request):
        raise AssertionError("must not call httpx for idempotent skip")
    _patch_httpx(monkeypatch, fail)

    ok = await import_base_image_by_id(session_factory, settings, bi.id)
    assert ok is True


@pytest.mark.asyncio
async def test_import_missing_row_returns_false(
    session_factory, storage_backend, tmp_path
):
    """Phantom target_id -> False, no error."""
    settings = _make_settings(tmp_path)
    ok = await import_base_image_by_id(
        session_factory, settings, uuid.uuid4()
    )
    assert ok is False


@pytest.mark.asyncio
async def test_import_disallowed_host_terminal(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    settings = _make_settings(tmp_path)
    bi = BaseImage(
        variant="pi5", version="v1",
        source_url="https://evil.example.com/agora-pi5.img.xz",
        expected_sha256="a" * 64,
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    def fail(request):
        raise AssertionError("must not reach network for disallowed host")
    _patch_httpx(monkeypatch, fail)

    with pytest.raises(TerminalImagerError, match="not in base_image_allowed_hosts"):
        await import_base_image_by_id(session_factory, settings, bi.id)

    async with session_factory() as db:
        fresh = (await db.execute(
            select(BaseImage).where(BaseImage.id == bi.id)
        )).scalar_one()
    assert fresh.status == BaseImageStatus.FAILED.value
    assert "evil.example.com" in fresh.error_message


@pytest.mark.asyncio
async def test_import_redirect_to_disallowed_host_terminal(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    """Allowed catalog url -> 302 to a disallowed host -> terminal."""
    settings = _make_settings(tmp_path)
    bi = BaseImage(
        variant="pi5", version="v1",
        source_url="https://github.com/redir/agora-pi5.img.xz",
        expected_sha256="a" * 64,
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    def handle(request: httpx.Request) -> httpx.Response:
        # Pretend GitHub redirects to an attacker-controlled mirror.
        return httpx.Response(
            302, headers={"location": "https://evil.example.com/x.img.xz"}
        )
    _patch_httpx(monkeypatch, handle)

    with pytest.raises(TerminalImagerError, match="not in base_image_allowed_hosts"):
        await import_base_image_by_id(session_factory, settings, bi.id)


@pytest.mark.asyncio
async def test_import_redirect_to_release_assets_cdn_succeeds(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    """github.com -> release-assets.githubusercontent.com is the real-world
    redirect path; the allowlist must permit it end-to-end."""
    payload = b"release-assets-cdn-payload" * 64
    expected_sha = hashlib.sha256(payload).hexdigest()
    settings = _make_settings(tmp_path)
    bi = BaseImage(
        variant="pi5", version="v1.11.28",
        source_url=("https://github.com/sslivins/agora/releases/"
                    "download/v1.11.28/agora-pi5.img.xz"),
        expected_sha256=expected_sha,
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    cdn_url = ("https://release-assets.githubusercontent.com/"
               "github-production-release-asset/abc/agora-pi5.img.xz")

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.host == "github.com":
            return httpx.Response(302, headers={"location": cdn_url})
        if request.url.host == "release-assets.githubusercontent.com":
            return httpx.Response(200, content=payload)
        return httpx.Response(404)
    _patch_httpx(monkeypatch, handle)

    ok = await import_base_image_by_id(session_factory, settings, bi.id)
    assert ok is True

    async with session_factory() as db:
        fresh = (await db.execute(
            select(BaseImage).where(BaseImage.id == bi.id)
        )).scalar_one()
    assert fresh.status == BaseImageStatus.READY.value
    assert fresh.sha256 == expected_sha


@pytest.mark.asyncio
async def test_import_sha_mismatch_terminal(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    settings = _make_settings(tmp_path)
    bi = BaseImage(
        variant="pi5", version="v1",
        source_url="https://github.com/foo/x.img.xz",
        expected_sha256="0" * 64,  # bytes won't hash to all-zero
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    _patch_httpx(monkeypatch, lambda r: httpx.Response(200, content=b"hello"))

    with pytest.raises(TerminalImagerError, match="sha256 mismatch"):
        await import_base_image_by_id(session_factory, settings, bi.id)

    async with session_factory() as db:
        fresh = (await db.execute(
            select(BaseImage).where(BaseImage.id == bi.id)
        )).scalar_one()
    assert fresh.status == BaseImageStatus.FAILED.value


@pytest.mark.asyncio
async def test_import_low_disk_retryable(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    """Below ``imager_min_free_bytes`` -> False (retryable), row stays IMPORTING."""
    settings = _make_settings(tmp_path, imager_min_free_bytes=10**18)
    bi = BaseImage(
        variant="pi5", version="v1",
        source_url="https://github.com/x.img.xz",
        expected_sha256="0" * 64,
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    def fail(request):
        raise AssertionError("must not reach network when low on disk")
    _patch_httpx(monkeypatch, fail)

    ok = await import_base_image_by_id(session_factory, settings, bi.id)
    assert ok is False

    async with session_factory() as db:
        fresh = (await db.execute(
            select(BaseImage).where(BaseImage.id == bi.id)
        )).scalar_one()
    assert fresh.status == BaseImageStatus.IMPORTING.value


@pytest.mark.asyncio
async def test_import_catalog_fallback(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    """No stamped URL -> resolve via catalog.json, then download."""
    from cms.services.imager_settings import set_catalog_url

    settings = _make_settings(tmp_path)
    payload = b"image-bytes-via-catalog-fallback" * 16
    expected_sha = hashlib.sha256(payload).hexdigest()
    image_url = ("https://objects.githubusercontent.com/foo/"
                 "agora-pi5.img.xz")
    catalog = {
        "ref": "v1.11.28",
        "variants": {
            "pi5": {
                "url": image_url,
                "sha256": expected_sha,
                "size_bytes": len(payload),
            },
        },
    }

    # PR 7: catalog URL lives in cms_settings now.
    await set_catalog_url(
        db_session,
        "https://github.com/sslivins/agora/releases/download/"
        "stable/catalog.json",
    )
    bi = BaseImage(variant="pi5", version="v1.11.28")  # no source_url
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    def handle(request: httpx.Request) -> httpx.Response:
        if str(request.url).endswith("catalog.json"):
            return httpx.Response(200, content=json.dumps(catalog))
        if request.url.host == "objects.githubusercontent.com":
            return httpx.Response(200, content=payload)
        raise AssertionError(f"unexpected url {request.url}")
    _patch_httpx(monkeypatch, handle)

    ok = await import_base_image_by_id(session_factory, settings, bi.id)
    assert ok is True

    async with session_factory() as db:
        fresh = (await db.execute(
            select(BaseImage).where(BaseImage.id == bi.id)
        )).scalar_one()
    assert fresh.status == BaseImageStatus.READY.value
    assert fresh.sha256 == expected_sha


# ── Provision handler ──────────────────────────────────────────────


async def _make_ready_base(db_session, *, sha: str, blob_path: str = "pi5/v1/base.img.xz"):
    """Insert a READY BaseImage row + write a fake blob to the storage
    backend.  Returns the row.  Caller has committed the session.
    """
    bi = BaseImage(
        variant="pi5", version="v1",
        status=BaseImageStatus.READY.value,
        sha256=sha, blob_path=blob_path, size_bytes=100,
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)
    return bi


def _stage_blob(backend: LocalStorageBackend, container: str, blob_name: str, data: bytes) -> None:
    """Lay down a blob under the LocalStorageBackend root."""
    target = Path(backend._base_path) / container / blob_name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


@pytest.mark.asyncio
async def test_provision_happy_path(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    """End-to-end: base READY, payload valid UTF-8, build_provisioned mocked."""
    settings = _make_settings(tmp_path)
    base_bytes = b"base-image-bytes" * 8
    base_sha = hashlib.sha256(base_bytes).hexdigest()
    bi = await _make_ready_base(db_session, sha=base_sha)
    _stage_blob(storage_backend, "base-images", "pi5/v1/base.img.xz", base_bytes)

    pi = ProvisionedImage(
        base_image_id=bi.id,
        output_name="agora-pi5-fleet42.img.xz",
        fleet_env_payload=b"AGORA_CMS_URL=https://cms.example\nAGORA_DEVICE_API_KEY=abc\n",
    )
    db_session.add(pi)
    await db_session.commit()
    await db_session.refresh(pi)

    def fake_build_provisioned(base_xz_path, fleet_env_text, scratch_dir, output_name):
        # Verify handler passed the right things
        assert Path(base_xz_path).read_bytes() == base_bytes
        assert "AGORA_DEVICE_API_KEY" in fleet_env_text
        assert output_name == "agora-pi5-fleet42.img.xz"
        out = Path(scratch_dir) / output_name
        out.write_bytes(b"final-image-payload")
        return out
    monkeypatch.setattr(imager_handlers, "build_provisioned", fake_build_provisioned)

    ok = await provision_image_by_id(session_factory, settings, pi.id)
    assert ok is True

    async with session_factory() as db:
        fresh = (await db.execute(
            select(ProvisionedImage).where(ProvisionedImage.id == pi.id)
        )).scalar_one()
    assert fresh.status == ProvisionedImageStatus.READY.value
    assert fresh.output_size == len(b"final-image-payload")
    assert fresh.output_sha256 == hashlib.sha256(b"final-image-payload").hexdigest()
    assert fresh.blob_path == f"{pi.id}/agora-pi5-fleet42.img.xz"
    assert fresh.built_at is not None
    assert fresh.expires_at is not None
    # Secret hygiene: payload cleared on success.
    assert fresh.fleet_env_payload is None

    assert await storage_backend.blob_exists(
        "provisioned", f"{pi.id}/agora-pi5-fleet42.img.xz"
    )


@pytest.mark.asyncio
async def test_provision_idempotent_when_already_ready(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    settings = _make_settings(tmp_path)
    bi = await _make_ready_base(db_session, sha="a" * 64)
    pi = ProvisionedImage(
        base_image_id=bi.id,
        output_name="x.img.xz",
        fleet_env_payload=b"k=v",
        status=ProvisionedImageStatus.READY.value,
    )
    db_session.add(pi)
    await db_session.commit()
    await db_session.refresh(pi)

    def fail(*args, **kwargs):
        raise AssertionError("build_provisioned must not run on idempotent skip")
    monkeypatch.setattr(imager_handlers, "build_provisioned", fail)

    ok = await provision_image_by_id(session_factory, settings, pi.id)
    assert ok is True


@pytest.mark.asyncio
async def test_provision_base_importing_retryable(
    db_session, session_factory, storage_backend, tmp_path
):
    """If base import is still in-flight, retry rather than fail."""
    settings = _make_settings(tmp_path)
    bi = BaseImage(variant="pi5", version="v1")  # status=IMPORTING by default
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    pi = ProvisionedImage(
        base_image_id=bi.id, output_name="x.img.xz",
        fleet_env_payload=b"k=v",
    )
    db_session.add(pi)
    await db_session.commit()
    await db_session.refresh(pi)

    ok = await provision_image_by_id(session_factory, settings, pi.id)
    assert ok is False

    async with session_factory() as db:
        fresh = (await db.execute(
            select(ProvisionedImage).where(ProvisionedImage.id == pi.id)
        )).scalar_one()
    # Retryable -> still PROVISIONING, payload retained for next try.
    assert fresh.status == ProvisionedImageStatus.PROVISIONING.value
    assert fresh.fleet_env_payload == b"k=v"


@pytest.mark.asyncio
async def test_provision_base_failed_terminal(
    db_session, session_factory, storage_backend, tmp_path
):
    settings = _make_settings(tmp_path)
    bi = BaseImage(
        variant="pi5", version="v1",
        status=BaseImageStatus.FAILED.value,
        error_message="upstream catalog returned 404",
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    pi = ProvisionedImage(
        base_image_id=bi.id, output_name="x.img.xz",
        fleet_env_payload=b"k=v",
    )
    db_session.add(pi)
    await db_session.commit()
    await db_session.refresh(pi)

    with pytest.raises(TerminalImagerError, match="cannot provision"):
        await provision_image_by_id(session_factory, settings, pi.id)

    async with session_factory() as db:
        fresh = (await db.execute(
            select(ProvisionedImage).where(ProvisionedImage.id == pi.id)
        )).scalar_one()
    assert fresh.status == ProvisionedImageStatus.FAILED.value
    # Terminal -> payload cleared.
    assert fresh.fleet_env_payload is None


@pytest.mark.asyncio
async def test_provision_base_sha_drift_terminal(
    db_session, session_factory, storage_backend, tmp_path
):
    """Cached blob bytes don't match BaseImage.sha256 -> tampering -> terminal."""
    settings = _make_settings(tmp_path)
    bi = await _make_ready_base(db_session, sha="b" * 64)
    _stage_blob(storage_backend, "base-images", "pi5/v1/base.img.xz", b"actual")

    pi = ProvisionedImage(
        base_image_id=bi.id, output_name="x.img.xz",
        fleet_env_payload=b"k=v",
    )
    db_session.add(pi)
    await db_session.commit()
    await db_session.refresh(pi)

    with pytest.raises(TerminalImagerError, match="drifted"):
        await provision_image_by_id(session_factory, settings, pi.id)

    async with session_factory() as db:
        fresh = (await db.execute(
            select(ProvisionedImage).where(ProvisionedImage.id == pi.id)
        )).scalar_one()
    assert fresh.status == ProvisionedImageStatus.FAILED.value
    assert fresh.fleet_env_payload is None


@pytest.mark.asyncio
async def test_provision_invalid_utf8_terminal(
    db_session, session_factory, storage_backend, tmp_path
):
    settings = _make_settings(tmp_path)
    base_bytes = b"x"
    base_sha = hashlib.sha256(base_bytes).hexdigest()
    bi = await _make_ready_base(db_session, sha=base_sha)
    _stage_blob(storage_backend, "base-images", "pi5/v1/base.img.xz", base_bytes)

    pi = ProvisionedImage(
        base_image_id=bi.id, output_name="x.img.xz",
        fleet_env_payload=b"\xff\xfe-not-utf8",
    )
    db_session.add(pi)
    await db_session.commit()
    await db_session.refresh(pi)

    with pytest.raises(TerminalImagerError, match="not valid utf-8"):
        await provision_image_by_id(session_factory, settings, pi.id)

    async with session_factory() as db:
        fresh = (await db.execute(
            select(ProvisionedImage).where(ProvisionedImage.id == pi.id)
        )).scalar_one()
    assert fresh.status == ProvisionedImageStatus.FAILED.value
    assert fresh.fleet_env_payload is None


@pytest.mark.asyncio
async def test_provision_low_disk_retryable(
    db_session, session_factory, storage_backend, tmp_path
):
    """Below threshold -> False (retryable), row + payload preserved."""
    settings = _make_settings(tmp_path, imager_min_free_bytes=10**18)
    bi = await _make_ready_base(db_session, sha="a" * 64)

    pi = ProvisionedImage(
        base_image_id=bi.id, output_name="x.img.xz",
        fleet_env_payload=b"k=v",
    )
    db_session.add(pi)
    await db_session.commit()
    await db_session.refresh(pi)

    ok = await provision_image_by_id(session_factory, settings, pi.id)
    assert ok is False

    async with session_factory() as db:
        fresh = (await db.execute(
            select(ProvisionedImage).where(ProvisionedImage.id == pi.id)
        )).scalar_one()
    assert fresh.status == ProvisionedImageStatus.PROVISIONING.value
    assert fresh.fleet_env_payload == b"k=v"


@pytest.mark.asyncio
async def test_provision_imager_error_terminal(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    """build_provisioned raises ImagerError -> TerminalImagerError, FAILED, payload cleared."""
    from cms.services.imager import ImagerError

    settings = _make_settings(tmp_path)
    base_bytes = b"base"
    base_sha = hashlib.sha256(base_bytes).hexdigest()
    bi = await _make_ready_base(db_session, sha=base_sha)
    _stage_blob(storage_backend, "base-images", "pi5/v1/base.img.xz", base_bytes)

    pi = ProvisionedImage(
        base_image_id=bi.id, output_name="x.img.xz",
        fleet_env_payload=b"AGORA_CMS_URL=foo\n",
    )
    db_session.add(pi)
    await db_session.commit()
    await db_session.refresh(pi)

    def boom(*args, **kwargs):
        raise ImagerError("xz decompression failed")
    monkeypatch.setattr(imager_handlers, "build_provisioned", boom)

    with pytest.raises(TerminalImagerError, match="imager pipeline failed"):
        await provision_image_by_id(session_factory, settings, pi.id)

    async with session_factory() as db:
        fresh = (await db.execute(
            select(ProvisionedImage).where(ProvisionedImage.id == pi.id)
        )).scalar_one()
    assert fresh.status == ProvisionedImageStatus.FAILED.value
    assert fresh.fleet_env_payload is None


@pytest.mark.asyncio
async def test_provision_missing_row_returns_false(
    session_factory, storage_backend, tmp_path
):
    settings = _make_settings(tmp_path)
    ok = await provision_image_by_id(
        session_factory, settings, uuid.uuid4()
    )
    assert ok is False


# ─────────────────────────────────────────────────────────────────────
# Import threshold (size-aware) tests
# ─────────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_size_aware_threshold_passes_when_free_space_enough(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    """expected_size known + free space ample -> threshold check passes
    (test stops at SHA mismatch from a tiny canned response)."""
    settings = _make_settings(tmp_path, imager_min_free_bytes=10 * 1024**3)
    bi = BaseImage(
        variant="pi5", version="v1",
        source_url="https://github.com/x.img.xz",
        expected_sha256="0" * 64,
        size_bytes=1_500_000_000,  # 1.5 GB
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    # 4 GiB free is plenty for a 1.5 GB import (needs 2*1.5 + 256 MiB).
    monkeypatch.setattr(
        imager_handlers, "_free_bytes",
        lambda *_: _async_return(4 * 1024**3),
    )
    _patch_httpx(monkeypatch, lambda r: httpx.Response(200, content=b"hi"))

    # The handler will pass the threshold check, download "hi", and fail
    # SHA verify (terminal).  We just want to confirm the threshold
    # check did not bail out early as retryable False.
    with pytest.raises(TerminalImagerError, match="sha256 mismatch"):
        await import_base_image_by_id(session_factory, settings, bi.id)


@pytest.mark.asyncio
async def test_import_size_aware_threshold_fails_when_free_space_low(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    """expected_size known + free space tight -> retryable False without
    touching the network."""
    # Set imager_min_free_bytes deliberately tiny: prove the import path
    # is using the size-derived threshold, not the env knob.
    settings = _make_settings(tmp_path, imager_min_free_bytes=1)
    bi = BaseImage(
        variant="pi5", version="v1",
        source_url="https://github.com/x.img.xz",
        expected_sha256="0" * 64,
        size_bytes=1_500_000_000,  # 1.5 GB -> needs ~3.25 GiB
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    # 1 GiB free is below the size-derived threshold.
    monkeypatch.setattr(
        imager_handlers, "_free_bytes",
        lambda *_: _async_return(1 * 1024**3),
    )

    def fail(request):
        raise AssertionError("must not reach network when low on disk")
    _patch_httpx(monkeypatch, fail)

    ok = await import_base_image_by_id(session_factory, settings, bi.id)
    assert ok is False

    async with session_factory() as db:
        fresh = (await db.execute(
            select(BaseImage).where(BaseImage.id == bi.id)
        )).scalar_one()
    assert fresh.status == BaseImageStatus.IMPORTING.value


@pytest.mark.asyncio
async def test_import_unknown_size_uses_conservative_fallback(
    db_session, session_factory, storage_backend, tmp_path, monkeypatch
):
    """When size_bytes is None, fall back to imager_min_free_bytes (10 GiB)
    rather than a tiny default that could let downloads fill the disk."""
    settings = _make_settings(tmp_path, imager_min_free_bytes=10 * 1024**3)
    bi = BaseImage(
        variant="pi5", version="v1",
        source_url="https://github.com/x.img.xz",
        expected_sha256="0" * 64,
        size_bytes=None,
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    # 5 GiB free: above any size-derived guess (would have passed at 2 GiB),
    # below the 10 GiB conservative fallback.
    monkeypatch.setattr(
        imager_handlers, "_free_bytes",
        lambda *_: _async_return(5 * 1024**3),
    )

    def fail(request):
        raise AssertionError("must not reach network with unknown size + low disk")
    _patch_httpx(monkeypatch, fail)

    ok = await import_base_image_by_id(session_factory, settings, bi.id)
    assert ok is False


# ─────────────────────────────────────────────────────────────────────
# mark_target_failed_on_exhaustion tests
# ─────────────────────────────────────────────────────────────────────


def _make_exhausted_job(target_id: uuid.UUID, job_type, *, retry_count=4,
                        error_message="last attempt: connection reset"):
    """Build a Job-shaped object with the fields the helper consumes.
    We don't need the row to be in DB -- the helper takes the job by
    value (it was just claimed, on its way to the dispatcher)."""
    from shared.models.job import Job, JobStatus
    j = Job(
        type=job_type,
        target_id=target_id,
        status=JobStatus.FAILED,
        retry_count=retry_count,
        error_message=error_message,
    )
    j.id = uuid.uuid4()
    return j


async def _async_return(value):
    return value


@pytest.mark.asyncio
async def test_poison_helper_flips_importing_base_image(
    db_session, session_factory, tmp_path
):
    """IMPORTING BaseImage + exhausted IMAGE_IMPORT job -> FAILED with
    descriptive error_message."""
    from shared.services.jobs import enqueue_job

    bi = BaseImage(
        variant="pi5", version="v1",
        source_url="https://x/y.img.xz",
        expected_sha256="0" * 64,
        status=BaseImageStatus.IMPORTING.value,
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    # Persist a job with this target_id so the latest-job guard finds
    # exactly one match.  enqueue_job uses target_id consistently.
    from shared.models.job import JobStatus
    job_id = await enqueue_job(db_session, JobType.IMAGE_IMPORT, bi.id)
    async with session_factory() as db:
        j = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
        j.retry_count = 4
        j.error_message = "transient network failure"
        j.status = JobStatus.FAILED
        await db.commit()
        job_row = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()

    result = await imager_handlers.mark_target_failed_on_exhaustion(
        session_factory, job_row,
    )
    assert result == "updated"

    async with session_factory() as db:
        fresh = (await db.execute(
            select(BaseImage).where(BaseImage.id == bi.id)
        )).scalar_one()
    assert fresh.status == BaseImageStatus.FAILED.value
    assert "exceeded retry limit" in (fresh.error_message or "")
    assert "4 attempts" in (fresh.error_message or "")
    assert "transient network failure" in (fresh.error_message or "")


@pytest.mark.asyncio
async def test_poison_helper_flips_provisioning_provisioned_image(
    db_session, session_factory, tmp_path
):
    """PROVISIONING ProvisionedImage + exhausted IMAGE_PROVISION job ->
    FAILED with descriptive error AND fleet_env_payload cleared."""
    from shared.services.jobs import enqueue_job
    from shared.models.job import Job, JobStatus

    bi = await _make_ready_base(db_session, sha="a" * 64)
    pi = ProvisionedImage(
        base_image_id=bi.id, output_name="x.img.xz",
        fleet_env_payload=b"AGORA_FLEET_SECRET=topsecret\n",
        status=ProvisionedImageStatus.PROVISIONING.value,
    )
    db_session.add(pi)
    await db_session.commit()
    await db_session.refresh(pi)

    job_id = await enqueue_job(db_session, JobType.IMAGE_PROVISION, pi.id)
    async with session_factory() as db:
        j = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
        j.retry_count = 4
        j.status = JobStatus.FAILED
        j.error_message = "build_provisioned subprocess crashed"
        await db.commit()
        job_row = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()

    result = await imager_handlers.mark_target_failed_on_exhaustion(
        session_factory, job_row,
    )
    assert result == "updated"

    async with session_factory() as db:
        fresh = (await db.execute(
            select(ProvisionedImage).where(ProvisionedImage.id == pi.id)
        )).scalar_one()
    assert fresh.status == ProvisionedImageStatus.FAILED.value
    assert fresh.fleet_env_payload is None
    assert "exceeded retry limit" in (fresh.error_message or "")


@pytest.mark.asyncio
async def test_poison_helper_skips_non_imager_job(session_factory):
    """VARIANT_TRANSCODE jobs are owned by the transcoder flow; the
    helper must no-op safely."""
    job = _make_exhausted_job(uuid.uuid4(), JobType.VARIANT_TRANSCODE)
    result = await imager_handlers.mark_target_failed_on_exhaustion(
        session_factory, job,
    )
    assert result == "not_imager"


@pytest.mark.asyncio
async def test_poison_helper_skips_when_target_row_missing(
    session_factory, tmp_path
):
    """Poison message arrives after the user deleted the BaseImage -> no-op."""
    job = _make_exhausted_job(uuid.uuid4(), JobType.IMAGE_IMPORT)
    result = await imager_handlers.mark_target_failed_on_exhaustion(
        session_factory, job,
    )
    assert result == "missing"


@pytest.mark.asyncio
async def test_poison_helper_does_not_clobber_ready_base_image(
    db_session, session_factory, tmp_path
):
    """Re-import after a previous FAILED reuses the same row UUID; if
    the new attempt has already succeeded (READY), a stale poison
    message from an OLDER attempt must NOT flip it back to FAILED."""
    from shared.services.jobs import enqueue_job
    from shared.models.job import Job, JobStatus

    bi = await _make_ready_base(db_session, sha="b" * 64)

    # Old job for this same target -- this is the one that "exhausted".
    old_id = await enqueue_job(db_session, JobType.IMAGE_IMPORT, bi.id)
    async with session_factory() as db:
        j = (await db.execute(select(Job).where(Job.id == old_id))).scalar_one()
        j.retry_count = 4
        j.status = JobStatus.FAILED
        j.error_message = "stale failure"
        await db.commit()

    # Newer job for the same target (the user re-imported).  Latest-job
    # guard should kick in.  Tiny sleep so created_at orders correctly
    # on filesystems where the resolution is coarse.
    import asyncio as _aio
    await _aio.sleep(0.01)
    new_id = await enqueue_job(db_session, JobType.IMAGE_IMPORT, bi.id)
    assert new_id != old_id

    async with session_factory() as db:
        old_row = (await db.execute(
            select(Job).where(Job.id == old_id)
        )).scalar_one()

    result = await imager_handlers.mark_target_failed_on_exhaustion(
        session_factory, old_row,
    )
    # Either skipped_newer_job (stale-job guard) or skipped_status
    # (status guard) -- both are correct outcomes.  We must not flip.
    assert result in ("skipped_newer_job", "skipped_status")

    async with session_factory() as db:
        fresh = (await db.execute(
            select(BaseImage).where(BaseImage.id == bi.id)
        )).scalar_one()
    assert fresh.status == BaseImageStatus.READY.value


@pytest.mark.asyncio
async def test_poison_helper_skips_when_status_is_not_in_flight(
    db_session, session_factory, tmp_path
):
    """If somehow the row has already moved past IMPORTING (e.g. another
    job concurrently set it READY), the status guard prevents clobber."""
    from shared.services.jobs import enqueue_job
    from shared.models.job import Job, JobStatus

    bi = BaseImage(
        variant="pi5", version="v1",
        source_url="https://x/y.img.xz",
        expected_sha256="0" * 64,
        # Already READY despite the job being the latest.
        status=BaseImageStatus.READY.value,
        sha256="c" * 64, blob_path="pi5/v1/base.img.xz", size_bytes=1,
    )
    db_session.add(bi)
    await db_session.commit()
    await db_session.refresh(bi)

    job_id = await enqueue_job(db_session, JobType.IMAGE_IMPORT, bi.id)
    async with session_factory() as db:
        j = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()
        j.retry_count = 4
        j.status = JobStatus.FAILED
        await db.commit()
        job_row = (await db.execute(select(Job).where(Job.id == job_id))).scalar_one()

    result = await imager_handlers.mark_target_failed_on_exhaustion(
        session_factory, job_row,
    )
    assert result == "skipped_status"

    async with session_factory() as db:
        fresh = (await db.execute(
            select(BaseImage).where(BaseImage.id == bi.id)
        )).scalar_one()
    assert fresh.status == BaseImageStatus.READY.value

