"""Tests for profile enable/disable (issue #237).

When a profile is disabled:
  * new asset uploads / new profile creation must NOT enqueue variants
    for that profile
  * existing READY variants stay intact (re-enable is instant)
  * in-flight / pending transcode jobs for that profile get cancelled

When it's re-enabled:
  * fan-out re-runs, so any assets uploaded while it was off now get variants
"""

import uuid

import pytest
from sqlalchemy import select


@pytest.mark.asyncio
class TestProfileEnableDisableApi:
    """API surface: POST /enable and /disable endpoints."""

    async def test_default_enabled_true(self, client, db_session):
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="new-default", video_codec="h264")
        db_session.add(profile)
        await db_session.commit()
        await db_session.refresh(profile)
        assert profile.enabled is True

    async def test_disable_then_enable_roundtrip(self, client, db_session):
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="toggle-me", video_codec="h264")
        db_session.add(profile)
        await db_session.commit()
        pid = profile.id

        r = await client.post(f"/api/profiles/{pid}/disable")
        assert r.status_code == 200
        assert r.json()["enabled"] is False

        db_session.expunge_all()
        got = await db_session.get(DeviceProfile, pid)
        assert got.enabled is False

        r = await client.post(f"/api/profiles/{pid}/enable")
        assert r.status_code == 200
        assert r.json()["enabled"] is True

        db_session.expunge_all()
        got = await db_session.get(DeviceProfile, pid)
        assert got.enabled is True

    async def test_disable_is_idempotent(self, client, db_session):
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="idempotent", video_codec="h264", enabled=False)
        db_session.add(profile)
        await db_session.commit()

        r = await client.post(f"/api/profiles/{profile.id}/disable")
        assert r.status_code == 200
        assert r.json()["enabled"] is False

    async def test_enable_is_idempotent(self, client, db_session):
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="idempotent-en", video_codec="h264")
        db_session.add(profile)
        await db_session.commit()

        r = await client.post(f"/api/profiles/{profile.id}/enable")
        assert r.status_code == 200
        assert r.json()["enabled"] is True

    async def test_enable_disable_404(self, client):
        fake = uuid.uuid4()
        r = await client.post(f"/api/profiles/{fake}/enable")
        assert r.status_code == 404
        r = await client.post(f"/api/profiles/{fake}/disable")
        assert r.status_code == 404

    async def test_profile_out_includes_enabled_field(self, client, db_session):
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(name="field-check", video_codec="h264")
        db_session.add(profile)
        await db_session.commit()

        r = await client.get("/api/profiles")
        assert r.status_code == 200
        body = r.json()
        entry = next(p for p in body if p["id"] == str(profile.id))
        assert entry["enabled"] is True


@pytest.mark.asyncio
class TestDisabledProfileSkipsTranscode:
    """Transcoder helpers must skip disabled profiles."""

    async def test_enqueue_for_new_profile_skips_disabled(self, client, db_session):
        """Profile created disabled: fan-out returns no variants even with
        existing video assets in the library."""
        from cms.models.asset import Asset, AssetType, AssetVariant
        from cms.models.device_profile import DeviceProfile
        from cms.services.transcoder import enqueue_for_new_profile

        asset = Asset(
            filename="existing.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum=f"skip-{uuid.uuid4().hex[:8]}",
        )
        db_session.add(asset)
        await db_session.flush()

        profile = DeviceProfile(
            name="disabled-at-create", video_codec="h264", enabled=False,
        )
        db_session.add(profile)
        await db_session.commit()

        ids = await enqueue_for_new_profile(profile.id, db_session)
        assert ids == []

        db_session.expunge_all()
        result = await db_session.execute(
            select(AssetVariant).where(AssetVariant.profile_id == profile.id)
        )
        assert result.scalars().all() == []

    async def test_asset_upload_skips_disabled_profile(self, client, db_session):
        """A disabled profile must NOT get a variant when a new asset is
        created via the transcoder fan-out path."""
        from cms.models.asset import Asset, AssetType, AssetVariant
        from cms.models.device_profile import DeviceProfile
        from cms.services.transcoder import _enqueue_transcoding_for_asset

        on_profile = DeviceProfile(
            name="on-upload", video_codec="h264", enabled=True,
        )
        off_profile = DeviceProfile(
            name="off-upload", video_codec="h264", enabled=False,
        )
        db_session.add_all([on_profile, off_profile])
        await db_session.flush()

        asset = Asset(
            filename="fresh.mp4", asset_type=AssetType.VIDEO,
            size_bytes=1000, checksum=f"upload-{uuid.uuid4().hex[:8]}",
        )
        db_session.add(asset)
        await db_session.commit()

        await _enqueue_transcoding_for_asset(asset, db_session)

        db_session.expunge_all()
        on_variants = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.profile_id == on_profile.id)
        )).scalars().all()
        off_variants = (await db_session.execute(
            select(AssetVariant).where(AssetVariant.profile_id == off_profile.id)
        )).scalars().all()

        assert len(on_variants) == 1, "enabled profile should get a variant"
        assert off_variants == [], "disabled profile must not get a variant"

    async def test_reenable_fanout_picks_up_missed_assets(
        self, client, db_session, tmp_path, monkeypatch,
    ):
        """After re-enabling, fan-out creates variants for assets that were
        uploaded while the profile was disabled."""
        from cms.models.asset import Asset, AssetType, AssetVariant
        from cms.models.device_profile import DeviceProfile

        profile = DeviceProfile(
            name="reenable-fanout", video_codec="h264", enabled=False,
        )
        db_session.add(profile)
        await db_session.flush()

        asset = Asset(
            filename="missed.mp4", asset_type=AssetType.VIDEO,
            size_bytes=2000, checksum=f"reen-{uuid.uuid4().hex[:8]}",
        )
        db_session.add(asset)
        await db_session.commit()
        await db_session.close()

        r = await client.post(f"/api/profiles/{profile.id}/enable")
        assert r.status_code == 200
        assert r.json()["enabled"] is True

        result = await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.profile_id == profile.id,
                AssetVariant.source_asset_id == asset.id,
            )
        )
        variants = result.scalars().all()
        assert len(variants) == 1, (
            f"re-enable should enqueue a variant for the missed asset, got {variants}"
        )

    async def test_upload_route_skips_disabled_profile(self, client, db_session):
        """Regression for #583: the HTTP upload route used to call a
        local `_enqueue_transcoding` that did NOT filter `enabled`, so
        disabling a profile didn't actually stop new uploads from getting
        variants for it. After consolidation onto
        `_enqueue_transcoding_for_asset` the upload path now skips
        disabled profiles like the rest of the fan-out."""
        import io
        from cms.models.asset import AssetVariant
        from cms.models.device_profile import DeviceProfile

        on_profile = DeviceProfile(
            name="upload-route-on", video_codec="h264", enabled=True,
        )
        off_profile = DeviceProfile(
            name="upload-route-off", video_codec="h264", enabled=False,
        )
        db_session.add_all([on_profile, off_profile])
        await db_session.commit()
        on_id, off_id = on_profile.id, off_profile.id

        files = {
            "file": (
                f"route-{uuid.uuid4().hex[:6]}.mp4",
                io.BytesIO(b"fakecontent"),
                "application/octet-stream",
            ),
        }
        resp = await client.post("/api/assets/upload", files=files)
        assert resp.status_code == 201, resp.text
        asset_id = uuid.UUID(resp.json()["id"])

        on_variants = (await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset_id,
                AssetVariant.profile_id == on_id,
            )
        )).scalars().all()
        off_variants = (await db_session.execute(
            select(AssetVariant).where(
                AssetVariant.source_asset_id == asset_id,
                AssetVariant.profile_id == off_id,
            )
        )).scalars().all()
        assert len(on_variants) == 1, "enabled profile should get a variant"
        assert off_variants == [], (
            "disabled profile must not get a variant from HTTP upload"
        )


@pytest.mark.asyncio
class TestProfileEnableDisableAuditLog:
    """Backend audit-log entries land for enable + disable actions."""

    async def test_disable_writes_audit_log(self, client, db_session):
        from cms.models.device_profile import DeviceProfile
        from cms.models.audit_log import AuditLog

        profile = DeviceProfile(name="audit-dis", video_codec="h264")
        db_session.add(profile)
        await db_session.commit()
        pid = profile.id

        r = await client.post(f"/api/profiles/{pid}/disable")
        assert r.status_code == 200

        rows = (await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "profile.disable")
            .where(AuditLog.resource_id == str(pid))
        )).scalars().all()
        assert len(rows) == 1

    async def test_enable_writes_audit_log(self, client, db_session):
        from cms.models.device_profile import DeviceProfile
        from cms.models.audit_log import AuditLog

        profile = DeviceProfile(name="audit-en", video_codec="h264", enabled=False)
        db_session.add(profile)
        await db_session.commit()
        pid = profile.id

        r = await client.post(f"/api/profiles/{pid}/enable")
        assert r.status_code == 200

        rows = (await db_session.execute(
            select(AuditLog)
            .where(AuditLog.action == "profile.enable")
            .where(AuditLog.resource_id == str(pid))
        )).scalars().all()
        assert len(rows) == 1


@pytest.mark.asyncio
class TestDisabledProfileDeviceAssignment:
    """Issue #583: device-assignment paths must reject disabled profiles.

    Surface inventory (see plan):
      * `PATCH /api/devices/{id}` -> 422 (and 404 for missing — pre-existing gap)
      * `POST /api/devices/{id}/adopt` -> 422
      * `device_bootstrap.adopt_pending_by_id` service -> ValueError("profile_disabled")
        (the bootstrap router exception mapping is covered in
        test_bootstrap_endpoints.py)
      * existing device keeps its assignment when the profile is later disabled
        (no cascade-clear).
    """

    async def _make_device(self, db_session, name="dev-assign", status=None):
        from cms.models.device import Device, DeviceStatus
        d = Device(
            id=f"{name}-{uuid.uuid4().hex[:6]}",
            name=name,
            status=status or DeviceStatus.ADOPTED,
        )
        db_session.add(d)
        await db_session.flush()
        return d

    async def _make_profile(self, db_session, name, *, enabled=True):
        from cms.models.device_profile import DeviceProfile
        p = DeviceProfile(name=name, video_codec="h264", enabled=enabled)
        db_session.add(p)
        await db_session.flush()
        return p

    async def test_patch_device_profile_rejects_disabled(self, client, db_session):
        device = await self._make_device(db_session, name="patch-dis")
        prof = await self._make_profile(db_session, "patch-disabled-prof", enabled=False)
        await db_session.commit()

        r = await client.patch(
            f"/api/devices/{device.id}",
            json={"profile_id": str(prof.id)},
        )
        assert r.status_code == 422
        assert "disabled" in r.json()["detail"].lower()

    async def test_patch_device_profile_404_when_missing(self, client, db_session):
        # Pre-existing gap folded in: PATCH used to silently accept a
        # nonexistent profile id (setattr with no validation). Now 404s.
        device = await self._make_device(db_session, name="patch-404")
        await db_session.commit()
        r = await client.patch(
            f"/api/devices/{device.id}",
            json={"profile_id": str(uuid.uuid4())},
        )
        assert r.status_code == 404

    async def test_patch_device_profile_accepts_enabled(self, client, db_session):
        # Sanity: the new validation must NOT break the happy path.
        device = await self._make_device(db_session, name="patch-ok")
        prof = await self._make_profile(db_session, "patch-enabled-prof")
        await db_session.commit()

        r = await client.patch(
            f"/api/devices/{device.id}",
            json={"profile_id": str(prof.id)},
        )
        assert r.status_code == 200
        await db_session.refresh(device)
        assert device.profile_id == prof.id

    async def test_adopt_existing_rejects_disabled(self, client, db_session):
        from cms.models.device import DeviceStatus
        device = await self._make_device(
            db_session, name="adopt-dis", status=DeviceStatus.PENDING,
        )
        prof = await self._make_profile(db_session, "adopt-disabled-prof", enabled=False)
        await db_session.commit()

        r = await client.post(
            f"/api/devices/{device.id}/adopt",
            json={"profile_id": str(prof.id)},
        )
        assert r.status_code == 422
        assert "disabled" in r.json()["detail"].lower()

    async def test_bootstrap_service_rejects_disabled(self, db_session):
        """`device_bootstrap.adopt_pending_by_id` raises
        ``ValueError("profile_disabled")`` when the requested profile is
        disabled. The bootstrap routers translate that into 422 — covered
        by tests in test_bootstrap_endpoints.py. This test guards the
        service contract directly so any future router change still has
        the disabled-profile check at the service boundary."""
        from cms.models.pending_registration import PendingRegistration
        from cms.services import device_bootstrap

        # Minimal pending row — only enough fields to reach the
        # profile-validation block at L486.
        pending = PendingRegistration(
            device_id=f"pi-svc-{uuid.uuid4().hex[:6]}",
            pubkey="dummy-pubkey",
            pairing_secret_hash="dummy-hash",
        )
        db_session.add(pending)
        prof = await self._make_profile(
            db_session, "svc-disabled-prof", enabled=False,
        )
        await db_session.commit()

        async def _mint(_device_row_id):  # pragma: no cover (not reached)
            return {}

        with pytest.raises(ValueError) as excinfo:
            await device_bootstrap.adopt_pending_by_id(
                db=db_session,
                pending_id=pending.id,
                profile_id=str(prof.id),
                name=None,
                location=None,
                group_id=None,
                mint_wps_jwt=_mint,
                settings=None,
            )
        assert str(excinfo.value) == "profile_disabled"

    async def test_existing_device_keeps_disabled_profile_assignment(
        self, client, db_session,
    ):
        """No cascade-clear: disabling a profile leaves already-assigned
        devices pointing at it. Only the FK column matters — there's no
        denormalized profile_name cache."""
        prof = await self._make_profile(db_session, "keepme-prof")
        device = await self._make_device(db_session, name="keep-assn")
        device.profile_id = prof.id
        await db_session.commit()
        pid = prof.id
        did = device.id

        r = await client.post(f"/api/profiles/{pid}/disable")
        assert r.status_code == 200

        db_session.expire_all()
        from cms.models.device import Device
        fresh = await db_session.get(Device, did)
        assert fresh.profile_id == pid, (
            "device.profile_id must remain pointing at the now-disabled profile"
        )

