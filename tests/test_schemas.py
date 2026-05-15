"""Tests for Pydantic schemas and protocol messages."""

import uuid

import pytest
from pydantic import ValidationError


class TestProtocolMessages:
    def test_register_message(self):
        from cms.schemas.protocol import RegisterMessage

        msg = RegisterMessage(
            device_id="pi-001",
            auth_token="token123",
            firmware_version="1.0.0",
            storage_capacity_mb=500,
            storage_used_mb=100,
        )
        data = msg.model_dump()
        assert data["type"] == "register"
        assert data["protocol_version"] == 2
        assert data["device_id"] == "pi-001"

    def test_sync_message_defaults(self):
        from cms.schemas.protocol import SyncMessage

        msg = SyncMessage()
        data = msg.model_dump(mode="json")
        assert data["type"] == "sync"
        assert data["protocol_version"] == 2

    def test_auth_assigned_message(self):
        from cms.schemas.protocol import AuthAssignedMessage

        msg = AuthAssignedMessage(device_auth_token="abc-123")
        data = msg.model_dump(mode="json")
        assert data["type"] == "auth_assigned"
        assert data["device_auth_token"] == "abc-123"

    def test_play_message(self):
        from cms.schemas.protocol import PlayMessage

        msg = PlayMessage(asset="video.mp4", loop=True)
        data = msg.model_dump(mode="json")
        assert data["type"] == "play"
        assert data["asset"] == "video.mp4"
        assert data["loop"] is True

    def test_fetch_asset_message(self):
        from cms.schemas.protocol import FetchAssetMessage

        msg = FetchAssetMessage(
            asset_name="promo.mp4",
            download_url="http://cms/api/assets/download/promo.mp4",
            checksum="sha256hash",
            size_bytes=1024,
        )
        data = msg.model_dump(mode="json")
        assert data["type"] == "fetch_asset"
        assert data["size_bytes"] == 1024


class TestOSUpdateDispatchMessage:
    """Tests for ``os_update_dispatch`` — the CMS→Device OS bundle dispatch."""

    def _valid_kwargs(self) -> dict:
        return dict(
            release_id="rel_2026_05_07_v1.1.0",
            target_version="1.1.0",
            min_from_version="1.0.0",
            bundle_url="https://example.com/bundle.zst",
            signature_url="https://example.com/bundle.zst.minisig",
        )

    def test_construction_defaults(self):
        from cms.schemas.protocol import OSUpdateDispatchMessage

        msg = OSUpdateDispatchMessage(**self._valid_kwargs())
        data = msg.model_dump(mode="json")
        assert data["type"] == "os_update_dispatch"
        assert data["protocol_version"] == 2
        assert data["release_id"] == "rel_2026_05_07_v1.1.0"
        assert data["target_version"] == "1.1.0"
        assert data["min_from_version"] == "1.0.0"
        assert data["bundle_url"] == "https://example.com/bundle.zst"
        assert data["signature_url"] == "https://example.com/bundle.zst.minisig"
        assert data["force_now"] is False
        assert data["force_downgrade"] is False
        assert data["not_before"] is None

    def test_explicit_optional_fields(self):
        from cms.schemas.protocol import OSUpdateDispatchMessage

        msg = OSUpdateDispatchMessage(
            **self._valid_kwargs(),
            force_now=True,
            force_downgrade=True,
            not_before="2026-05-07T02:00:00Z",
        )
        data = msg.model_dump(mode="json")
        assert data["force_now"] is True
        assert data["force_downgrade"] is True
        assert data["not_before"] == "2026-05-07T02:00:00Z"

    def test_version_v_prefix_rejected(self):
        from cms.schemas.protocol import OSUpdateDispatchMessage

        kwargs = self._valid_kwargs()
        kwargs["target_version"] = "v1.1.0"
        with pytest.raises(ValidationError):
            OSUpdateDispatchMessage(**kwargs)

    def test_partial_version_rejected(self):
        from cms.schemas.protocol import OSUpdateDispatchMessage

        kwargs = self._valid_kwargs()
        kwargs["target_version"] = "1.1"
        with pytest.raises(ValidationError):
            OSUpdateDispatchMessage(**kwargs)

    def test_prerelease_version_accepted(self):
        from cms.schemas.protocol import OSUpdateDispatchMessage

        kwargs = self._valid_kwargs()
        kwargs["target_version"] = "1.1.0-test"
        OSUpdateDispatchMessage(**kwargs)

    def test_release_id_bad_chars_rejected(self):
        from cms.schemas.protocol import OSUpdateDispatchMessage

        kwargs = self._valid_kwargs()
        kwargs["release_id"] = "release id with spaces"
        with pytest.raises(ValidationError):
            OSUpdateDispatchMessage(**kwargs)

    def test_release_id_too_long_rejected(self):
        from cms.schemas.protocol import OSUpdateDispatchMessage

        kwargs = self._valid_kwargs()
        kwargs["release_id"] = "a" * 129
        with pytest.raises(ValidationError):
            OSUpdateDispatchMessage(**kwargs)

    def test_non_http_url_rejected(self):
        from cms.schemas.protocol import OSUpdateDispatchMessage

        kwargs = self._valid_kwargs()
        kwargs["bundle_url"] = "ftp://example.com/bundle.zst"
        with pytest.raises(ValidationError):
            OSUpdateDispatchMessage(**kwargs)

    def test_roundtrip_json(self):
        from cms.schemas.protocol import OSUpdateDispatchMessage

        msg = OSUpdateDispatchMessage(**self._valid_kwargs())
        as_json = msg.model_dump_json()
        rebuilt = OSUpdateDispatchMessage.model_validate_json(as_json)
        assert rebuilt.model_dump() == msg.model_dump()

    def test_regex_strings_match_vendored_device_side(self):
        """Drift-detector: if these strings differ, the wire contract is broken."""
        from cms.schemas import protocol
        from tests.contract import device_dispatch_validator

        assert (
            protocol._OS_UPDATE_DISPATCH_VERSION_RE.pattern
            == device_dispatch_validator._VERSION_RE.pattern
        ), "CMS-side version regex drifted from device-side; refresh vendored copy"
        assert (
            protocol._OS_UPDATE_DISPATCH_RELEASE_ID_RE.pattern
            == device_dispatch_validator._RELEASE_ID_RE.pattern
        ), "CMS-side release_id regex drifted from device-side; refresh vendored copy"

    def test_contract_roundtrip_through_device_validator(self):
        """Build CMS msg → JSON → strip envelope → validate as device side."""
        import json

        from cms.schemas.protocol import OSUpdateDispatchMessage
        from tests.contract.device_dispatch_validator import DispatchPayload

        cms_msg = OSUpdateDispatchMessage(
            **self._valid_kwargs(),
            force_now=True,
            not_before="2026-05-07T02:00:00Z",
        )

        wire = json.loads(cms_msg.model_dump_json())
        wire.pop("type", None)
        wire.pop("protocol_version", None)

        device_payload = DispatchPayload.model_validate(wire)
        assert device_payload.release_id == cms_msg.release_id
        assert device_payload.target_version == cms_msg.target_version
        assert device_payload.min_from_version == cms_msg.min_from_version
        assert device_payload.bundle_url == cms_msg.bundle_url
        assert device_payload.signature_url == cms_msg.signature_url
        assert device_payload.force_now is True
        assert device_payload.force_downgrade is False
        assert device_payload.not_before == "2026-05-07T02:00:00Z"


class TestDeviceSchemas:
    def test_device_out(self):
        from cms.schemas.device import DeviceOut

        data = DeviceOut(
            id="pi-001",
            name="Kitchen",
            status="adopted",
            firmware_version="1.0",
            storage_capacity_mb=500,
            storage_used_mb=100,
            registered_at="2025-01-01T00:00:00Z",
        )
        assert data.id == "pi-001"
        assert data.default_asset_id is None

    def test_device_update_partial(self):
        from cms.schemas.device import DeviceUpdate

        update = DeviceUpdate(name="New Name")
        dumped = update.model_dump(exclude_unset=True)
        assert "name" in dumped
        assert "status" not in dumped
        assert "group_id" not in dumped

    def test_device_group_create(self):
        from cms.schemas.device import DeviceGroupCreate

        group = DeviceGroupCreate(name="Lobby")
        assert group.description == ""
        assert group.default_asset_id is None


class TestScheduleSchemas:
    def test_schedule_create_requires_target(self):
        from cms.schemas.schedule import ScheduleCreate

        with pytest.raises(ValidationError):
            ScheduleCreate(
                name="No Target",
                asset_id=uuid.uuid4(),
                start_time="08:00",
                end_time="12:00",
            )

    def test_schedule_create_with_group(self):
        from cms.schemas.schedule import ScheduleCreate

        sched = ScheduleCreate(
            name="Valid",
            group_id=uuid.uuid4(),
            asset_id=uuid.uuid4(),
            start_time="08:00",
            end_time="12:00",
        )
        assert sched.enabled is True
        assert sched.priority == 0

    def test_schedule_update_partial(self):
        from cms.schemas.schedule import ScheduleUpdate

        update = ScheduleUpdate(enabled=False)
        dumped = update.model_dump(exclude_unset=True)
        assert dumped == {"enabled": False}
