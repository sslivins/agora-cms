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
        assert data["protocol_version"] == 1
        assert data["device_id"] == "pi-001"

    def test_sync_message_defaults(self):
        from cms.schemas.protocol import SyncMessage

        msg = SyncMessage()
        data = msg.model_dump(mode="json")
        assert data["type"] == "sync"
        assert data["protocol_version"] == 1

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

    def test_schedule_create_rejects_both_targets(self):
        from cms.schemas.schedule import ScheduleCreate

        with pytest.raises(ValidationError):
            ScheduleCreate(
                name="Both",
                device_id="pi-001",
                group_id=uuid.uuid4(),
                asset_id=uuid.uuid4(),
                start_time="08:00",
                end_time="12:00",
            )

    def test_schedule_create_with_device(self):
        from cms.schemas.schedule import ScheduleCreate

        sched = ScheduleCreate(
            name="Valid",
            device_id="pi-001",
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
