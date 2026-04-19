"""Tests for display_connected / error transition -> DeviceEvent emission (#122)."""

import hashlib
import time

import pytest
from sqlalchemy import select

from cms.models.device import Device, DeviceStatus
from cms.models.device_event import DeviceEvent, DeviceEventType


def _make_adopted_device(device_id: str) -> tuple[Device, str]:
    token = f"tok-{device_id}"
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    dev = Device(
        id=device_id,
        name=device_id,
        status=DeviceStatus.ADOPTED,
        device_auth_token_hash=token_hash,
    )
    return dev, token


def _register(ws, device_id, token):
    ws.send_json({
        "type": "register",
        "protocol_version": 1,
        "device_id": device_id,
        "auth_token": token,
        "firmware_version": "1.0.0",
        "storage_capacity_mb": 500,
    })
    # Consume sync + config (adopted device)
    ws.receive_json()
    ws.receive_json()


def _status(ws, device_id, **kwargs):
    payload = {
        "type": "status",
        "device_id": device_id,
        "mode": "splash",
        "storage_used_mb": 100,
    }
    payload.update(kwargs)
    ws.send_json(payload)


@pytest.mark.asyncio
class TestDeviceEventTransitions:
    async def test_display_transition_emits_events(self, app, db_session):
        from starlette.testclient import TestClient

        dev, token = _make_adopted_device("evt-display-001")
        db_session.add(dev)
        await db_session.commit()
        await db_session.close()

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                _register(ws, "evt-display-001", token)
                # First observation: display_connected=True (no prior -> no event)
                _status(ws, "evt-display-001", display_connected=True)
                time.sleep(0.3)
                # Flip to False -> DISPLAY_DISCONNECTED
                _status(ws, "evt-display-001", display_connected=False)
                time.sleep(0.3)
                # Flip back to True -> DISPLAY_CONNECTED
                _status(ws, "evt-display-001", display_connected=True)
                time.sleep(0.3)
                ws.close()

        rows = (await db_session.execute(
            select(DeviceEvent)
            .where(DeviceEvent.device_id == "evt-display-001")
            .order_by(DeviceEvent.created_at.asc())
        )).scalars().all()
        types = [r.event_type for r in rows]
        assert DeviceEventType.DISPLAY_DISCONNECTED.value in types
        assert DeviceEventType.DISPLAY_CONNECTED.value in types
        # First status should NOT have emitted anything (None -> True)
        # so we expect exactly 2 display transitions.
        display_rows = [r for r in rows if r.event_type.startswith("display_")]
        assert len(display_rows) == 2

    async def test_error_set_and_cleared_emits_events(self, app, db_session):
        from starlette.testclient import TestClient

        dev, token = _make_adopted_device("evt-err-001")
        db_session.add(dev)
        await db_session.commit()
        await db_session.close()

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                _register(ws, "evt-err-001", token)
                # Clean status (no prior, no error) — no event
                _status(ws, "evt-err-001")
                time.sleep(0.3)
                # Error appears
                _status(ws, "evt-err-001", error="pipeline stalled")
                time.sleep(0.3)
                # Error cleared
                _status(ws, "evt-err-001", error=None)
                time.sleep(0.3)
                ws.close()

        rows = (await db_session.execute(
            select(DeviceEvent)
            .where(DeviceEvent.device_id == "evt-err-001")
            .order_by(DeviceEvent.created_at.asc())
        )).scalars().all()
        err_rows = [r for r in rows if r.event_type in (DeviceEventType.ERROR.value, DeviceEventType.ERROR_CLEARED.value)]
        assert len(err_rows) == 2
        assert err_rows[0].event_type == DeviceEventType.ERROR.value
        assert err_rows[0].details and err_rows[0].details.get("error") == "pipeline stalled"
        assert err_rows[1].event_type == DeviceEventType.ERROR_CLEARED.value

    async def test_error_string_change_emits_new_error_event(self, app, db_session):
        from starlette.testclient import TestClient

        dev, token = _make_adopted_device("evt-err-002")
        db_session.add(dev)
        await db_session.commit()
        await db_session.close()

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                _register(ws, "evt-err-002", token)
                _status(ws, "evt-err-002", error="first fault")
                time.sleep(0.3)
                # Different error string while still in error state → another ERROR event
                _status(ws, "evt-err-002", error="second fault")
                time.sleep(0.3)
                ws.close()

        rows = (await db_session.execute(
            select(DeviceEvent)
            .where(DeviceEvent.device_id == "evt-err-002")
            .order_by(DeviceEvent.created_at.asc())
        )).scalars().all()
        err_rows = [r for r in rows if r.event_type == DeviceEventType.ERROR.value]
        assert len(err_rows) == 2
        assert err_rows[-1].details.get("error") == "second fault"

    async def test_no_event_on_stable_state(self, app, db_session):
        """Repeated identical status messages must NOT emit spurious events."""
        from starlette.testclient import TestClient

        dev, token = _make_adopted_device("evt-stable-001")
        db_session.add(dev)
        await db_session.commit()
        await db_session.close()

        with TestClient(app) as tc:
            with tc.websocket_connect("/ws/device") as ws:
                _register(ws, "evt-stable-001", token)
                for _ in range(3):
                    _status(ws, "evt-stable-001", display_connected=True)
                    time.sleep(0.15)
                ws.close()

        rows = (await db_session.execute(
            select(DeviceEvent)
            .where(DeviceEvent.device_id == "evt-stable-001")
        )).scalars().all()
        display_rows = [r for r in rows if r.event_type.startswith("display_")]
        err_rows = [r for r in rows if r.event_type in (DeviceEventType.ERROR.value, DeviceEventType.ERROR_CLEARED.value)]
        assert display_rows == []
        assert err_rows == []
