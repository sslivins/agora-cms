"""API tests for the browser-driven Pi image provisioning router (PR 4).

Covers RBAC, schema validation, catalog wiring, idempotent imports,
delete-with-references guard, build payload + audit, job polling,
and download SAS handing.

Catalog HTTP fetches are mocked via ``httpx.MockTransport`` so tests
do not hit the real upstream.  Worker job execution is *not* exercised
here -- ``test_imager_handlers.py`` covers that.  These tests verify
that the API layer correctly enqueues, validates, and reads back job
state.
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import select

from cms.auth import get_settings
from cms.routers import imager as imager_router
from shared.models.imager import (
    BaseImage,
    BaseImageStatus,
    ProvisionedImage,
    ProvisionedImageStatus,
)
from shared.models.job import Job, JobStatus, JobType

from tests.test_rbac import _create_user, _login_as


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def imager_settings(app):
    """Configure the test settings with imager fields populated.

    Mutates the singleton settings instance the app fixture installed
    via ``dependency_overrides``.  Restores defaults on teardown.

    NOTE: PR 7 moved the catalog URL out of ``Settings`` and into the
    ``cms_settings`` table.  Tests that need a URL configured should
    use the :func:`imager_catalog_url` fixture as well.
    """
    settings = app.dependency_overrides[get_settings]()
    saved = {
        "base_image_allowed_hosts": settings.base_image_allowed_hosts,
        "base_image_cache_container": settings.base_image_cache_container,
        "provisioned_container": settings.provisioned_container,
        "imager_sas_ttl_hours": settings.imager_sas_ttl_hours,
        "fleet_register_secrets": dict(settings.fleet_register_secrets or {}),
        "base_url": settings.base_url,
    }
    settings.base_image_allowed_hosts = "github.com,objects.githubusercontent.com"
    settings.base_image_cache_container = "base-images"
    settings.provisioned_container = "provisioned"
    settings.imager_sas_ttl_hours = 2
    settings.fleet_register_secrets = {"fleet-test": "c2VjcmV0LWJhc2U2NA=="}
    settings.base_url = "https://cms.example.com"
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)


@pytest_asyncio.fixture
async def imager_catalog_url(db_session):
    """Seed the imager catalog URL setting in the DB.

    PR 7 moved the URL out of ``Settings``.  Tests that exercise the
    catalog or base-image-import endpoints need this fixture; tests
    that exercise the unset/503 path should NOT request it.
    """
    from cms.services.imager_settings import set_catalog_url

    url = (
        "https://github.com/sslivins/agora/releases/download/v1.11.28/catalog.json"
    )
    await set_catalog_url(db_session, url)
    await db_session.commit()
    return url


def _catalog_doc(ref: str = "v1.11.28") -> dict[str, Any]:
    return {
        "ref": ref,
        "variants": {
            "pi5": {
                "url": "https://github.com/sslivins/agora/releases/download/"
                       f"{ref}/agora-pi5.img.xz",
                "sha256": "a" * 64,
                "size_bytes": 600 * 1024 * 1024,
            },
            "pi4": {
                "url": "https://github.com/sslivins/agora/releases/download/"
                       f"{ref}/agora-pi4.img.xz",
                "sha256": "b" * 64,
                "size_bytes": 600 * 1024 * 1024,
            },
        },
    }


@pytest.fixture
def patch_catalog_ok(monkeypatch, imager_settings, imager_catalog_url):
    """Make every httpx call from the imager router return the canned catalog."""
    body = json.dumps(_catalog_doc()).encode("utf-8")

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=body, headers={"content-type": "application/json"})

    transport = httpx.MockTransport(_handler)
    real_client_cls = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(imager_router.httpx, "AsyncClient", _factory)


@pytest.fixture
def patch_catalog_error(monkeypatch, imager_settings, imager_catalog_url):
    """Make catalog fetches fail with a network error."""
    def _handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("test simulated network error")

    transport = httpx.MockTransport(_handler)
    real_client_cls = httpx.AsyncClient

    def _factory(*args, **kwargs):
        kwargs.setdefault("transport", transport)
        return real_client_cls(*args, **kwargs)

    monkeypatch.setattr(imager_router.httpx, "AsyncClient", _factory)


# ── Auth / RBAC ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_endpoints_require_auth(unauthed_client):
    """Every imager endpoint must reject unauthenticated requests."""
    paths = [
        ("GET", "/api/imager/fleets"),
        ("GET", "/api/imager/catalog"),
        ("GET", "/api/imager/base-images"),
        ("POST", "/api/imager/base-images"),
        ("DELETE", f"/api/imager/base-images/{uuid.uuid4()}"),
        ("POST", "/api/imager/build"),
        ("GET", f"/api/imager/jobs/{uuid.uuid4()}"),
        ("GET", f"/api/imager/download/{uuid.uuid4()}"),
    ]
    for method, path in paths:
        kwargs = {"json": {}} if method == "POST" else {}
        resp = await unauthed_client.request(method, path, **kwargs)
        assert resp.status_code == 401, f"{method} {path} got {resp.status_code}"


@pytest.mark.asyncio
async def test_viewer_denied_imager_read(app, db_session):
    """Viewer role has no IMAGER_READ — every endpoint returns 403."""
    await _create_user(db_session, email="viewer-img@test.com", role_name="Viewer")
    ac = await _login_as(app, "viewer-img@test.com")
    try:
        for method, path in [
            ("GET", "/api/imager/fleets"),
            ("GET", "/api/imager/base-images"),
        ]:
            resp = await ac.request(method, path)
            assert resp.status_code == 403, f"{method} {path}: {resp.status_code}"
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_operator_denied_imager_manage(app, db_session, imager_settings):
    """Operator has IMAGER_READ + IMAGER_BUILD but not IMAGER_MANAGE."""
    await _create_user(db_session, email="op-img@test.com", role_name="Operator")
    ac = await _login_as(app, "op-img@test.com")
    try:
        # MANAGE-gated:
        resp = await ac.get("/api/imager/catalog")
        assert resp.status_code == 403
        resp = await ac.post("/api/imager/base-images", json={"variant": "pi5", "version": "v1"})
        assert resp.status_code == 403
        # READ-gated should pass:
        resp = await ac.get("/api/imager/fleets")
        assert resp.status_code == 200
        resp = await ac.get("/api/imager/base-images")
        assert resp.status_code == 200
    finally:
        await ac.aclose()


# ── Fleets ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_fleets_returns_configured_ids_no_secrets(client, imager_settings):
    resp = await client.get("/api/imager/fleets")
    assert resp.status_code == 200
    data = resp.json()
    assert data == [{"fleet_id": "fleet-test"}]
    # Sanity: secret is never serialized.
    assert "secret" not in resp.text.lower()


# ── Catalog ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_catalog_503_when_url_unconfigured(client, imager_settings):
    # Default DB state: no imager.catalog_url row set.
    resp = await client.get("/api/imager/catalog")
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_catalog_502_on_network_error(client, patch_catalog_error):
    resp = await client.get("/api/imager/catalog")
    assert resp.status_code == 502


@pytest.mark.asyncio
async def test_catalog_happy_path(client, patch_catalog_ok):
    resp = await client.get("/api/imager/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert body["ref"] == "v1.11.28"
    variants = {e["variant"] for e in body["entries"]}
    assert variants == {"pi5", "pi4"}


# ── Base-image import ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_creates_row_and_stamps_catalog(client, db_session, patch_catalog_ok):
    resp = await client.post(
        "/api/imager/base-images",
        json={"variant": "pi5", "version": "v1.11.28"},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["variant"] == "pi5"
    assert body["version"] == "v1.11.28"
    assert body["status"] == BaseImageStatus.IMPORTING.value
    # DB row stamped with catalog coords.
    row = (await db_session.execute(
        select(BaseImage).where(BaseImage.id == uuid.UUID(body["id"]))
    )).scalar_one()
    assert row.source_url and "agora-pi5.img.xz" in row.source_url
    assert row.expected_sha256 == "a" * 64
    # Job row enqueued.
    job = (await db_session.execute(
        select(Job).where(Job.target_id == row.id, Job.type == JobType.IMAGE_IMPORT)
    )).scalar_one()
    assert job.status == JobStatus.PENDING


@pytest.mark.asyncio
async def test_import_idempotent_when_row_already_ready(
    client, db_session, patch_catalog_ok
):
    bi = BaseImage(
        variant="pi5",
        version="v1.11.28",
        sha256="a" * 64,
        blob_path="base-images/pi5/v1.11.28/base.img.xz",
        size_bytes=1234,
        status=BaseImageStatus.READY.value,
    )
    db_session.add(bi)
    await db_session.commit()

    resp = await client.post(
        "/api/imager/base-images",
        json={"variant": "pi5", "version": "v1.11.28"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(bi.id)
    assert body["status"] == BaseImageStatus.READY.value
    # No job should have been enqueued for an already-READY row.
    jobs = (await db_session.execute(
        select(Job).where(Job.target_id == bi.id, Job.type == JobType.IMAGE_IMPORT)
    )).scalars().all()
    assert jobs == []


@pytest.mark.asyncio
async def test_import_restarts_failed_row(client, db_session, patch_catalog_ok):
    bi = BaseImage(
        variant="pi5",
        version="v1.11.28",
        status=BaseImageStatus.FAILED.value,
        error_message="prior failure",
        source_url="https://old/example.img.xz",
        expected_sha256="z" * 64,
    )
    db_session.add(bi)
    await db_session.commit()

    resp = await client.post(
        "/api/imager/base-images",
        json={"variant": "pi5", "version": "v1.11.28"},
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(bi.id)
    assert body["status"] == BaseImageStatus.IMPORTING.value
    assert (body["error_message"] or "") == ""
    # Source URL re-stamped from catalog.
    await db_session.refresh(bi)
    assert "agora-pi5.img.xz" in bi.source_url
    assert bi.expected_sha256 == "a" * 64


@pytest.mark.asyncio
async def test_import_422_on_ref_mismatch(client, patch_catalog_ok):
    resp = await client.post(
        "/api/imager/base-images",
        json={"variant": "pi5", "version": "v9.99.99"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_import_404_on_unknown_variant(client, patch_catalog_ok):
    resp = await client.post(
        "/api/imager/base-images",
        json={"variant": "nope", "version": "v1.11.28"},
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_import_503_when_catalog_url_unconfigured(client, imager_settings):
    # Default DB state: no imager.catalog_url row set.
    resp = await client.post(
        "/api/imager/base-images",
        json={"variant": "pi5", "version": "v1.11.28"},
    )
    assert resp.status_code == 503


# ── Base-image list ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_list_base_images(client, db_session, imager_settings):
    db_session.add(BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value))
    db_session.add(BaseImage(variant="pi4", version="v1", status=BaseImageStatus.IMPORTING.value))
    await db_session.commit()

    resp = await client.get("/api/imager/base-images")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 2
    # API contract: status field is lowercase on the wire. The frontend
    # normalizes via statusUp(); breaking this contract silently breaks
    # auto-refresh, transition toasts, dropdown filters, etc.
    statuses = sorted(b["status"] for b in data)
    assert statuses == ["importing", "ready"]


# ── Base-image delete ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_404_when_missing(client, imager_settings):
    resp = await client.delete(f"/api/imager/base-images/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_409_when_referenced(client, db_session, imager_settings):
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.flush()
    pi = ProvisionedImage(
        base_image_id=bi.id,
        output_name="x.img.xz",
        fleet_env_payload=b"AGORA_CMS_URL=https://x\nAGORA_FLEET_ID=f\nAGORA_FLEET_SECRET=s\n",
        status=ProvisionedImageStatus.READY.value,
    )
    db_session.add(pi)
    await db_session.commit()

    resp = await client.delete(f"/api/imager/base-images/{bi.id}")
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_delete_succeeds_when_unreferenced(client, db_session, imager_settings):
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.commit()

    resp = await client.delete(f"/api/imager/base-images/{bi.id}")
    assert resp.status_code == 204
    # Row gone.
    assert (await db_session.execute(select(BaseImage).where(BaseImage.id == bi.id))).scalar_one_or_none() is None


# ── Build ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_422_on_invalid_output_name(client, db_session, imager_settings):
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.commit()
    resp = await client.post(
        "/api/imager/build",
        json={
            "base_image_id": str(bi.id),
            "fleet_id": "fleet-test",
            "output_name": "../etc/passwd",
        },
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_build_404_when_fleet_unknown(client, db_session, imager_settings):
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.commit()
    resp = await client.post(
        "/api/imager/build",
        json={
            "base_image_id": str(bi.id),
            "fleet_id": "no-such-fleet",
            "output_name": "x.img.xz",
        },
    )
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_build_503_when_base_url_missing(client, db_session, imager_settings):
    imager_settings.base_url = None
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.commit()
    resp = await client.post(
        "/api/imager/build",
        json={
            "base_image_id": str(bi.id),
            "fleet_id": "fleet-test",
            "output_name": "x.img.xz",
        },
    )
    assert resp.status_code == 503


@pytest.mark.asyncio
async def test_build_409_when_base_not_ready(client, db_session, imager_settings):
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.IMPORTING.value)
    db_session.add(bi)
    await db_session.commit()
    resp = await client.post(
        "/api/imager/build",
        json={
            "base_image_id": str(bi.id),
            "fleet_id": "fleet-test",
            "output_name": "x.img.xz",
        },
    )
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_build_creates_provisioned_row_with_payload(
    client, db_session, imager_settings
):
    bi = BaseImage(variant="pi5", version="v1.11.28", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.commit()

    resp = await client.post(
        "/api/imager/build",
        json={
            "base_image_id": str(bi.id),
            "fleet_id": "fleet-test",
            "output_name": "kitchen.img.xz",
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == JobStatus.PENDING.value
    assert body["type"] == JobType.IMAGE_PROVISION.value
    assert body["output_name"] == "kitchen.img.xz"

    # Provisioned row has the plaintext payload + fleet_id.
    pi = (await db_session.execute(
        select(ProvisionedImage).where(ProvisionedImage.base_image_id == bi.id)
    )).scalar_one()
    assert pi.fleet_id == "fleet-test"
    assert pi.output_name == "kitchen.img.xz"
    payload = pi.fleet_env_payload.decode("utf-8")
    assert "AGORA_CMS_URL=https://cms.example.com" in payload
    assert "AGORA_FLEET_ID=fleet-test" in payload
    assert "AGORA_FLEET_SECRET=c2VjcmV0LWJhc2U2NA==" in payload  # gitleaks:allow

    # And a Job row was enqueued.
    job = (await db_session.execute(
        select(Job).where(Job.target_id == pi.id, Job.type == JobType.IMAGE_PROVISION)
    )).scalar_one()
    assert job.status == JobStatus.PENDING


# ── Jobs / download ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_get_job_404_for_non_imager_job(client, imager_settings):
    resp = await client.get(f"/api/imager/jobs/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_get_job_returns_status(client, db_session, imager_settings):
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.IMPORTING.value)
    db_session.add(bi)
    await db_session.flush()
    job = Job(type=JobType.IMAGE_IMPORT, target_id=bi.id, status=JobStatus.PENDING)
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/imager/jobs/{job.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == JobStatus.PENDING.value
    # API contract: lowercase on the wire. Frontend depends on this.
    assert body["status"] == "pending"
    assert body["type"] == JobType.IMAGE_IMPORT.value
    assert body["download_url"] is None


@pytest.mark.asyncio
async def test_get_job_includes_download_url_when_ready(
    client, db_session, imager_settings
):
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.flush()
    pi = ProvisionedImage(
        base_image_id=bi.id,
        output_name="x.img.xz",
        fleet_env_payload=b"AGORA_CMS_URL=https://x\n",
        status=ProvisionedImageStatus.READY.value,
        blob_path="provisioned/abc/x.img.xz",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db_session.add(pi)
    await db_session.flush()
    job = Job(
        type=JobType.IMAGE_PROVISION, target_id=pi.id,
        status=JobStatus.DONE,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/imager/jobs/{job.id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["download_url"]
    assert body["output_name"] == "x.img.xz"


@pytest.mark.asyncio
async def test_download_404_when_not_ready(client, db_session, imager_settings):
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.flush()
    pi = ProvisionedImage(
        base_image_id=bi.id,
        output_name="x.img.xz",
        fleet_env_payload=b"x\n",
        status=ProvisionedImageStatus.PROVISIONING.value,
    )
    db_session.add(pi)
    await db_session.flush()
    job = Job(type=JobType.IMAGE_PROVISION, target_id=pi.id, status=JobStatus.PENDING)
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/imager/download/{job.id}", follow_redirects=False)
    # Job not SUCCEEDED → 409 ('not ready').
    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_download_redirects_when_ready(
    client, db_session, imager_settings, monkeypatch, tmp_path,
):
    """Download endpoint hands back a 302 once everything is in place."""
    # Stub blob_exists+SAS so the test does not depend on the local-backend
    # ``file://`` URL (httpx rejects Windows paths in Location headers).
    from shared.services import storage as storage_mod

    async def _blob_exists(self, container: str, blob_name: str) -> bool:
        return True

    def _sas(self, container: str, blob_name: str, ttl_hours: int) -> str:
        return f"https://example.com/sas/{container}/{blob_name}"

    monkeypatch.setattr(
        storage_mod.LocalStorageBackend, "blob_exists", _blob_exists,
    )
    monkeypatch.setattr(
        storage_mod.LocalStorageBackend, "generate_blob_sas_url", _sas,
    )

    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.flush()
    pi = ProvisionedImage(
        base_image_id=bi.id,
        output_name="x.img.xz",
        fleet_env_payload=b"x\n",
        status=ProvisionedImageStatus.READY.value,
        blob_path="abc/x.img.xz",
        expires_at=datetime.now(timezone.utc) + timedelta(hours=24),
    )
    db_session.add(pi)
    await db_session.flush()
    job = Job(
        type=JobType.IMAGE_PROVISION, target_id=pi.id,
        status=JobStatus.DONE,
    )
    db_session.add(job)
    await db_session.commit()

    resp = await client.get(f"/api/imager/download/{job.id}", follow_redirects=False)
    assert resp.status_code == 302, resp.text
    assert resp.headers.get("location", "").startswith("https://example.com/sas/")


# ── Audit ──────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_build_writes_audit_row(client, db_session, imager_settings):
    from cms.models.audit_log import AuditLog
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.commit()

    resp = await client.post(
        "/api/imager/build",
        json={
            "base_image_id": str(bi.id),
            "fleet_id": "fleet-test",
            "output_name": "x.img.xz",
        },
    )
    assert resp.status_code == 200

    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "imager.build")
    )).scalars().all()
    assert len(rows) == 1
    details = rows[0].details or {}
    # Ensure the secret is NOT in the audit details.
    assert "c2VjcmV0LWJhc2U2NA==" not in str(details)
    assert details.get("fleet_id") == "fleet-test"


@pytest.mark.asyncio
async def test_import_writes_audit_row(client, db_session, patch_catalog_ok):
    from cms.models.audit_log import AuditLog
    resp = await client.post(
        "/api/imager/base-images",
        json={"variant": "pi5", "version": "v1.11.28"},
    )
    assert resp.status_code == 200
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "imager.base_image.import")
    )).scalars().all()
    assert len(rows) == 1
