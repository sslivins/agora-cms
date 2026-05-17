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

import base64
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


@pytest_asyncio.fixture
async def imager_settings(app, db_session):
    """Configure the test settings with imager fields populated.

    Mutates the singleton settings instance the app fixture installed
    via ``dependency_overrides``.  Restores defaults on teardown.

    Also seeds a ``fleet-test`` row in the ``fleets`` table so the
    Imager build endpoint can resolve a fleet secret via
    ``fleet_registry``.

    NOTE: PR 7 moved the catalog URL out of ``Settings`` and into the
    ``cms_settings`` table.  Tests that need a URL configured should
    use the :func:`imager_catalog_url` fixture as well.
    """
    from shared.models.fleet import Fleet

    settings = app.dependency_overrides[get_settings]()
    saved = {
        "base_image_allowed_hosts": settings.base_image_allowed_hosts,
        "base_image_cache_container": settings.base_image_cache_container,
        "provisioned_container": settings.provisioned_container,
        "imager_sas_ttl_hours": settings.imager_sas_ttl_hours,
        "base_url": settings.base_url,
    }
    settings.base_image_allowed_hosts = "github.com,objects.githubusercontent.com"
    settings.base_image_cache_container = "base-images"
    settings.provisioned_container = "provisioned"
    settings.imager_sas_ttl_hours = 2
    settings.base_url = "https://cms.example.com"

    fleet = Fleet(
        fleet_id="fleet-test",
        # Generate at runtime so a literal base64 blob doesn't trip
        # gitleaks' generic-api-key rule on this hard-coded fixture.
        secret_b64=base64.b64encode(b"secret-base64").decode("ascii"),
        description="imager test fixture fleet",
    )
    db_session.add(fleet)
    await db_session.commit()
    yield settings
    for k, v in saved.items():
        setattr(settings, k, v)
    await db_session.delete(fleet)
    await db_session.commit()


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
        ("GET", "/api/imager/provisioned-images"),
        ("DELETE", f"/api/imager/provisioned-images/{uuid.uuid4()}"),
        ("GET", f"/api/imager/jobs/{uuid.uuid4()}"),
        ("GET", f"/api/imager/download/{uuid.uuid4()}"),
        ("GET", f"/api/imager/download-url/{uuid.uuid4()}"),
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
    # Progress fields default to empty stage / null pct when no
    # in-flight job is present.
    for row in data:
        assert row["progress_stage"] == ""
        assert row["progress_pct"] is None


@pytest.mark.asyncio
async def test_list_base_images_surfaces_in_flight_progress(
    client, db_session, imager_settings,
):
    """IMPORTING rows expose the latest in-flight import job's progress.

    The ``imager.html`` table renders a ``badge-progress`` cell when
    ``progress_pct`` is non-null; this test pins the wire contract so
    UI-side renderers can rely on the field always being there for
    in-flight rows.
    """
    importing = BaseImage(variant="pi5", version="v9", status=BaseImageStatus.IMPORTING.value)
    ready = BaseImage(variant="pi4", version="v9", status=BaseImageStatus.READY.value)
    db_session.add_all([importing, ready])
    await db_session.flush()
    # In-flight job for the importing row.
    db_session.add(Job(
        type=JobType.IMAGE_IMPORT,
        target_id=importing.id,
        status=JobStatus.PROCESSING,
        progress_stage="downloading",
        progress_pct=42,
    ))
    # Old, terminal job for the same row -- must not be picked up.
    db_session.add(Job(
        type=JobType.IMAGE_IMPORT,
        target_id=importing.id,
        status=JobStatus.FAILED,
        progress_stage="aborted",
        progress_pct=99,
    ))
    await db_session.commit()

    resp = await client.get("/api/imager/base-images")
    assert resp.status_code == 200
    rows = {r["variant"]: r for r in resp.json()}
    # In-flight row exposes the live job's progress.
    assert rows["pi5"]["progress_stage"] == "downloading"
    assert rows["pi5"]["progress_pct"] == 42
    # Terminal/READY rows do not surface stale progress.
    assert rows["pi4"]["progress_stage"] == ""
    assert rows["pi4"]["progress_pct"] is None


# ── Base-image delete ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_404_when_missing(client, imager_settings):
    resp = await client.delete(f"/api/imager/base-images/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_base_image_succeeds_when_referenced_nulls_fk(
    client, db_session, imager_settings,
):
    """Deleting a base referenced by built images succeeds.

    Built ``.img.xz`` artifacts are fully self-contained, so a base
    deletion only nulls the audit FK on the dependent
    ``ProvisionedImage`` rows -- the ``base_variant`` /
    ``base_version`` snapshot columns preserve identity.
    """
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.flush()
    pi = ProvisionedImage(
        base_image_id=bi.id,
        base_variant=bi.variant,
        base_version=bi.version,
        output_name="x.img.xz",
        fleet_env_payload=b"AGORA_CMS_URL=https://x\nAGORA_FLEET_ID=f\nAGORA_FLEET_SECRET=s\n",
        status=ProvisionedImageStatus.READY.value,
    )
    db_session.add(pi)
    await db_session.commit()
    pi_id = pi.id
    bi_id = bi.id

    resp = await client.delete(f"/api/imager/base-images/{bi_id}")
    assert resp.status_code == 204

    # Base row is gone; audit row survives with nulled FK + snapshot.
    db_session.expire_all()
    assert (
        await db_session.execute(select(BaseImage).where(BaseImage.id == bi_id))
    ).scalar_one_or_none() is None
    pi_after = (
        await db_session.execute(select(ProvisionedImage).where(ProvisionedImage.id == pi_id))
    ).scalar_one()
    assert pi_after.base_image_id is None
    assert pi_after.base_variant == "pi5"
    assert pi_after.base_version == "v1"


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
    detail = resp.json()["detail"]
    assert "AGORA_CMS_BASE_URL" in detail
    assert "/ws/device" in detail or "register" in detail


@pytest.mark.asyncio
async def test_build_succeeds_in_wps_mode_with_transport_baked(
    client, db_session, imager_settings
):
    """WPS-mode CMS bakes AGORA_CMS_TRANSPORT=wps into the fleet env."""
    saved_transport = imager_settings.device_transport
    try:
        imager_settings.device_transport = "wps"
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
        assert resp.status_code == 200, resp.text
        pi = (await db_session.execute(
            select(ProvisionedImage).where(ProvisionedImage.base_image_id == bi.id)
        )).scalar_one()
        payload = pi.fleet_env_payload.decode("utf-8")
        assert "AGORA_CMS_URL=wss://cms.example.com/ws/device" in payload
        assert "AGORA_CMS_TRANSPORT=wps" in payload
    finally:
        imager_settings.device_transport = saved_transport


@pytest.mark.asyncio
async def test_build_503_when_device_transport_invalid(
    client, db_session, imager_settings
):
    """Anything other than local/wps should refuse cleanly."""
    saved_transport = imager_settings.device_transport
    try:
        imager_settings.device_transport = "carrier-pigeon"
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
        assert "carrier-pigeon" in resp.json()["detail"]
    finally:
        imager_settings.device_transport = saved_transport


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
    # Audit snapshot of the base image is populated at build time so
    # the row survives an admin deleting the base image later.
    assert pi.base_variant == "pi5"
    assert pi.base_version == "v1.11.28"
    payload = pi.fleet_env_payload.decode("utf-8")
    # AGORA_CMS_BASE_URL=https://cms.example.com → wss://cms.example.com/ws/device
    # The firmware passes this directly to websockets.connect() and derives
    # the HTTPS API base by swapping the scheme back.
    assert "AGORA_CMS_URL=wss://cms.example.com/ws/device" in payload
    assert "AGORA_CMS_TRANSPORT=direct" in payload
    assert "AGORA_FLEET_ID=fleet-test" in payload
    # Firmware (agora-fleet-provision.sh) allow-lists AGORA_FLEET_SECRET_HEX
    # only and hex-decodes the value.  Stored secret is base64 of raw bytes
    # (b"secret-base64") -> hex below.
    assert "AGORA_FLEET_SECRET_HEX=7365637265742d626173653634" in payload
    assert "AGORA_FLEET_SECRET=" not in payload.replace(
        "AGORA_FLEET_SECRET_HEX=", ""
    )

    # And a Job row was enqueued.
    job = (await db_session.execute(
        select(Job).where(Job.target_id == pi.id, Job.type == JobType.IMAGE_PROVISION)
    )).scalar_one()
    assert job.status == JobStatus.PENDING
    # And the producing job is denormalized onto the row so the
    # built-images list endpoint can surface it for the Download
    # button without a reverse join.
    await db_session.refresh(pi)
    assert pi.provisioning_job_id == job.id


@pytest.mark.asyncio
async def test_build_persists_wifi_columns_when_supplied(
    client, db_session, imager_settings
):
    """End-to-end: wifi values flow from request -> payload -> row.

    Pin the contract so a regression in the schema, the helper, or the
    persistence step is caught immediately.  The columns are NOT
    cleared on terminal success (unlike fleet_env_payload), so the
    Built-Images Wi-Fi tooltip can keep showing them for the lifetime
    of the row.
    """
    bi = BaseImage(variant="pi5", version="v1.11.35", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.commit()

    resp = await client.post(
        "/api/imager/build",
        json={
            "base_image_id": str(bi.id),
            "fleet_id": "fleet-test",
            "output_name": "kitchen-wifi.img.xz",
            "wifi_ssid": "kitchen-net",
            "wifi_psk": "hunter22hunter22",
        },
    )
    assert resp.status_code == 200, resp.text

    pi = (await db_session.execute(
        select(ProvisionedImage).where(ProvisionedImage.base_image_id == bi.id)
    )).scalar_one()
    assert pi.wifi_ssid == "kitchen-net"
    assert pi.wifi_psk == "hunter22hunter22"
    payload = pi.fleet_env_payload.decode("utf-8")
    assert "AGORA_WIFI_SSID=kitchen-net" in payload
    assert "AGORA_WIFI_PASS=hunter22hunter22" in payload


@pytest.mark.asyncio
async def test_build_no_wifi_keeps_columns_null(client, db_session, imager_settings):
    """Default path: omitting wifi leaves both columns NULL.

    Distinguishes "no wifi requested" from "" -- empty strings would
    confuse the Built-Images tooltip's truthy-check renderer.
    """
    bi = BaseImage(variant="pi5", version="v1.11.35", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.commit()

    resp = await client.post(
        "/api/imager/build",
        json={
            "base_image_id": str(bi.id),
            "fleet_id": "fleet-test",
            "output_name": "wired.img.xz",
        },
    )
    assert resp.status_code == 200, resp.text

    pi = (await db_session.execute(
        select(ProvisionedImage).where(ProvisionedImage.base_image_id == bi.id)
    )).scalar_one()
    assert pi.wifi_ssid is None
    assert pi.wifi_psk is None
    assert b"AGORA_WIFI_" not in pi.fleet_env_payload


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload,reason",
    [
        ({"wifi_ssid": "net", "wifi_psk": None}, "ssid without psk"),
        ({"wifi_ssid": None, "wifi_psk": "hunter22hunter22"}, "psk without ssid"),
        ({"wifi_ssid": "net", "wifi_psk": ""}, "empty psk"),
        ({"wifi_ssid": "", "wifi_psk": "hunter22hunter22"}, "empty ssid"),
        ({"wifi_ssid": "net", "wifi_psk": "short"}, "psk under 8 chars"),
        ({"wifi_ssid": "x" * 33, "wifi_psk": "hunter22hunter22"}, "ssid over 32 chars"),
    ],
)
async def test_build_422_on_wifi_pair_mismatch(
    client, db_session, imager_settings, payload, reason
):
    """Schema rejects half-set / out-of-range wifi pairs at the door.

    Pushes the both-or-neither / length validation into pydantic so the
    build endpoint can assume the values are coherent.  Without this,
    a half-set pair would silently emit a payload the firmware refuses,
    and the Pi would brick its first-boot wifi join.
    """
    bi = BaseImage(variant="pi5", version="v1.11.35", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.commit()

    body = {
        "base_image_id": str(bi.id),
        "fleet_id": "fleet-test",
        "output_name": "bad-wifi.img.xz",
        **payload,
    }
    resp = await client.post("/api/imager/build", json=body)
    assert resp.status_code == 422, f"{reason}: {resp.status_code} / {resp.text}"


def test_fleet_env_payload_matches_firmware_allowlist():
    """Regression for the 2026-05-06 ``test-fleet-1`` outage.

    The firmware-side ``agora-fleet-provision.sh`` allow-lists exactly
    three keys: ``AGORA_FLEET_ID``, ``AGORA_FLEET_SECRET_HEX``,
    ``AGORA_BOOTSTRAP_V2``.  Anything else is dropped silently.  The
    secret value is hex-decoded.

    Pin the imager output so we can never again ship images where the
    firmware silently drops the credential.
    """
    from cms.routers.imager import _fleet_env_payload

    raw = b"\x00\x01\x02\x03decafbad" + b"x" * 20

    out = _fleet_env_payload(
        "wss://cms.example.com/ws/device", "fleet-x", raw, "wps"
    ).decode("utf-8")

    # Key name must be the firmware-expected ``_HEX`` form.
    assert "AGORA_FLEET_SECRET_HEX=" in out
    # Pre-fix imager wrote ``AGORA_FLEET_SECRET=<base64>``.  Make sure
    # the legacy bad key name doesn't sneak back into the file.
    lines = [ln for ln in out.splitlines() if "=" in ln]
    keys = {ln.split("=", 1)[0] for ln in lines}
    assert "AGORA_FLEET_SECRET" not in keys

    # Value is hex(raw_bytes).
    hex_line = next(ln for ln in lines if ln.startswith("AGORA_FLEET_SECRET_HEX="))
    assert hex_line.split("=", 1)[1] == raw.hex()


def test_fleet_env_payload_emits_wifi_when_provided():
    """Wi-Fi env keys must match the firmware allow-list exactly.

    Firmware (agora v1.11.35+) added ``AGORA_WIFI_SSID`` /
    ``AGORA_WIFI_PASS`` to the agora-fleet-provision.sh allow-list.
    Earlier design docs called the password key
    ``AGORA_WIFI_PASSPHRASE`` -- the firmware spec is authoritative.

    Pin the keys so the next person who edits the design doc can't
    silently drift the wire format.
    """
    from cms.routers.imager import _fleet_env_payload

    out = _fleet_env_payload(
        "wss://cms.example.com/ws/device",
        "fleet-x",
        b"x" * 32,
        "wps",
        wifi_ssid="my-net",
        wifi_psk="hunter22hunter22",
    ).decode("utf-8")

    assert "AGORA_WIFI_SSID=my-net" in out
    assert "AGORA_WIFI_PASS=hunter22hunter22" in out
    # Legacy mis-naming MUST NOT leak in.
    assert "AGORA_WIFI_PASSPHRASE" not in out


def test_fleet_env_payload_omits_wifi_when_unset():
    """No wifi keys when neither / only one is provided.

    Build endpoint enforces both-or-neither at the schema layer, but
    pin the helper's behaviour too -- the helper is reachable directly
    from worker / tests and must default to a clean payload.
    """
    from cms.routers.imager import _fleet_env_payload

    base = _fleet_env_payload(
        "wss://cms/ws/device", "f", b"x" * 32, "wps"
    ).decode("utf-8")
    assert "AGORA_WIFI_" not in base

    # Only ssid -> still no wifi (defensive: should not happen via API).
    only_ssid = _fleet_env_payload(
        "wss://cms/ws/device", "f", b"x" * 32, "wps", wifi_ssid="net",
    ).decode("utf-8")
    assert "AGORA_WIFI_" not in only_ssid

    only_psk = _fleet_env_payload(
        "wss://cms/ws/device", "f", b"x" * 32, "wps", wifi_psk="hunter22hunter22",
    ).decode("utf-8")
    assert "AGORA_WIFI_" not in only_psk


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


@pytest.mark.asyncio
async def test_download_url_returns_sas_as_json(
    client, db_session, imager_settings, monkeypatch,
):
    """``/download-url/{job_id}`` hands back the SAS as JSON for wget-style copy."""
    from cms.models.audit_log import AuditLog
    from shared.services import storage as storage_mod

    async def _blob_exists(self, container: str, blob_name: str) -> bool:
        return True

    def _sas(self, container: str, blob_name: str, ttl_hours: int) -> str:
        return f"https://example.com/sas/{container}/{blob_name}?sig=stub"

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

    resp = await client.get(f"/api/imager/download-url/{job.id}")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["url"].startswith("https://example.com/sas/")
    assert "?sig=" in body["url"]

    # Audit row is logged under a distinct action so the audit trail
    # can distinguish "copied the link" from "actually downloaded".
    rows = (await db_session.execute(
        select(AuditLog).where(AuditLog.action == "imager.copy_link")
    )).scalars().all()
    assert len(rows) == 1
    assert rows[0].resource_id == str(pi.id)


@pytest.mark.asyncio
async def test_download_url_404_when_not_ready(
    client, db_session, imager_settings,
):
    """Same not-ready guards as ``/download/{job_id}`` (job pending → 409)."""
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

    resp = await client.get(f"/api/imager/download-url/{job.id}")
    assert resp.status_code == 409


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


# ── _derive_device_ws_url unit tests ──────────────────────────────


def test_derive_device_ws_url_https_to_wss():
    from cms.routers.imager import _derive_device_ws_url

    assert _derive_device_ws_url("https://agora.example.com") == "wss://agora.example.com/ws/device"


def test_derive_device_ws_url_http_to_ws():
    from cms.routers.imager import _derive_device_ws_url

    assert _derive_device_ws_url("http://192.168.1.10:8080") == "ws://192.168.1.10:8080/ws/device"


def test_derive_device_ws_url_strips_trailing_slash():
    from cms.routers.imager import _derive_device_ws_url

    assert _derive_device_ws_url("https://agora.example.com/") == "wss://agora.example.com/ws/device"


def test_derive_device_ws_url_preserves_port():
    from cms.routers.imager import _derive_device_ws_url

    assert (
        _derive_device_ws_url("https://cms.example.com:8443")
        == "wss://cms.example.com:8443/ws/device"
    )


def test_derive_device_ws_url_rejects_empty():
    from cms.routers.imager import _derive_device_ws_url

    with pytest.raises(ValueError):
        _derive_device_ws_url("")


def test_derive_device_ws_url_rejects_no_host():
    from cms.routers.imager import _derive_device_ws_url

    with pytest.raises(ValueError):
        _derive_device_ws_url("not-a-url")


def test_derive_device_ws_url_rejects_path():
    from cms.routers.imager import _derive_device_ws_url

    with pytest.raises(ValueError):
        _derive_device_ws_url("https://agora.example.com/some/path")


def test_derive_device_ws_url_rejects_query():
    from cms.routers.imager import _derive_device_ws_url

    with pytest.raises(ValueError):
        _derive_device_ws_url("https://agora.example.com?x=1")


def test_derive_device_ws_url_rejects_unsupported_scheme():
    from cms.routers.imager import _derive_device_ws_url

    with pytest.raises(ValueError):
        _derive_device_ws_url("ftp://agora.example.com")


# ── Built-images list & delete ─────────────────────────────────────


@pytest.mark.asyncio
async def test_list_provisioned_images_returns_rows_newest_first(
    client, db_session, imager_settings,
):
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.flush()

    older = ProvisionedImage(
        base_image_id=bi.id,
        base_variant="pi5",
        base_version="v1",
        output_name="older.img.xz",
        fleet_id="fleet-test",
        status=ProvisionedImageStatus.READY.value,
        blob_path="provisioned/older.img.xz",
        output_size=12345,
    )
    newer = ProvisionedImage(
        base_image_id=bi.id,
        base_variant="pi5",
        base_version="v1",
        output_name="newer.img.xz",
        fleet_id="fleet-test",
        status=ProvisionedImageStatus.READY.value,
        blob_path="provisioned/newer.img.xz",
        output_size=23456,
    )
    db_session.add_all([older, newer])
    await db_session.flush()
    # Force created_at ordering deterministically.
    older.created_at = datetime.now(timezone.utc) - timedelta(hours=1)
    newer.created_at = datetime.now(timezone.utc)
    await db_session.commit()

    resp = await client.get("/api/imager/provisioned-images")
    assert resp.status_code == 200
    rows = resp.json()
    names = [r["output_name"] for r in rows]
    assert names.index("newer.img.xz") < names.index("older.img.xz")

    # Schema sanity.
    row = next(r for r in rows if r["output_name"] == "newer.img.xz")
    assert row["base_variant"] == "pi5"
    assert row["base_version"] == "v1"
    assert row["fleet_id"] == "fleet-test"
    assert row["output_size"] == 23456
    assert row["status"] == ProvisionedImageStatus.READY.value


@pytest.mark.asyncio
async def test_list_provisioned_images_surfaces_in_flight_progress(
    client, db_session, imager_settings,
):
    """PROVISIONING rows expose the latest in-flight provision job's progress.

    Mirrors the base-image test: drives the ``badge-progress`` render
    on the built-images panel.  Pins both the field shape and the
    "ignore terminal jobs" behavior of ``_latest_in_flight_progress``.
    """
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.flush()

    building = ProvisionedImage(
        base_image_id=bi.id,
        base_variant="pi5",
        base_version="v1",
        output_name="building.img.xz",
        fleet_id="fleet-test",
        status=ProvisionedImageStatus.PROVISIONING.value,
    )
    ready = ProvisionedImage(
        base_image_id=bi.id,
        base_variant="pi5",
        base_version="v1",
        output_name="ready.img.xz",
        fleet_id="fleet-test",
        status=ProvisionedImageStatus.READY.value,
        blob_path="provisioned/ready.img.xz",
        output_size=23456,
    )
    db_session.add_all([building, ready])
    await db_session.flush()
    # Live PROCESSING job for the in-flight row.
    db_session.add(Job(
        type=JobType.IMAGE_PROVISION,
        target_id=building.id,
        status=JobStatus.PROCESSING,
        progress_stage="compressing",
        progress_pct=72,
    ))
    # Older terminal job for the same target -- must not be picked.
    db_session.add(Job(
        type=JobType.IMAGE_PROVISION,
        target_id=building.id,
        status=JobStatus.DONE,
        progress_stage="archived",
        progress_pct=100,
    ))
    await db_session.commit()

    resp = await client.get("/api/imager/provisioned-images")
    assert resp.status_code == 200
    rows = {r["output_name"]: r for r in resp.json()}
    # In-flight row exposes live progress; terminal row does not.
    assert rows["building.img.xz"]["progress_stage"] == "compressing"
    assert rows["building.img.xz"]["progress_pct"] == 72
    assert rows["ready.img.xz"]["progress_stage"] == ""
    assert rows["ready.img.xz"]["progress_pct"] is None


@pytest.mark.asyncio
async def test_list_provisioned_images_403_for_viewer(app, db_session):
    """IMAGER_BUILD permission is required to list built images."""
    await _create_user(db_session, email="viewer-pi@test.com", role_name="Viewer")
    ac = await _login_as(app, "viewer-pi@test.com")
    try:
        resp = await ac.get("/api/imager/provisioned-images")
        assert resp.status_code == 403
    finally:
        await ac.aclose()


@pytest.mark.asyncio
async def test_delete_provisioned_image_admin_succeeds(
    client, db_session, imager_settings,
):
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.flush()
    pi = ProvisionedImage(
        base_image_id=bi.id,
        base_variant="pi5",
        base_version="v1",
        output_name="todelete.img.xz",
        fleet_id="fleet-test",
        status=ProvisionedImageStatus.READY.value,
        blob_path="provisioned/todelete.img.xz",
    )
    db_session.add(pi)
    await db_session.commit()
    pi_id = pi.id

    resp = await client.delete(f"/api/imager/provisioned-images/{pi_id}")
    assert resp.status_code == 204

    db_session.expire_all()
    assert (
        await db_session.execute(
            select(ProvisionedImage).where(ProvisionedImage.id == pi_id)
        )
    ).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_delete_provisioned_image_404_when_missing(client, imager_settings):
    resp = await client.delete(f"/api/imager/provisioned-images/{uuid.uuid4()}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_provisioned_image_403_for_operator(
    app, db_session, imager_settings,
):
    """Operator (IMAGER_BUILD only) cannot delete built images."""
    bi = BaseImage(variant="pi5", version="v1", status=BaseImageStatus.READY.value)
    db_session.add(bi)
    await db_session.flush()
    pi = ProvisionedImage(
        base_image_id=bi.id,
        base_variant="pi5",
        base_version="v1",
        output_name="forbidden.img.xz",
        fleet_id="fleet-test",
        status=ProvisionedImageStatus.READY.value,
    )
    db_session.add(pi)
    await db_session.commit()

    await _create_user(db_session, email="op-del@test.com", role_name="Operator")
    ac = await _login_as(app, "op-del@test.com")
    try:
        resp = await ac.delete(f"/api/imager/provisioned-images/{pi.id}")
        assert resp.status_code == 403
    finally:
        await ac.aclose()

