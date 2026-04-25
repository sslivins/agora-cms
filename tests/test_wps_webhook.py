"""Unit tests for the WPS upstream-webhook receiver.

These tests wire up only the webhook router (not the full CMS app) with
``get_db`` overridden to yield a fake async session, so they run
without Postgres and without the rest of the lifespan machinery.  The
``Settings`` lookup is monkeypatched to a hand-built instance so tests
can control the WPS connection string (and therefore the signing key).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient


WPS_KEY = "test-webhook-key"
CONN_STR = f"Endpoint=http://broker:7080;AccessKey={WPS_KEY};Version=1.0;"


def _sig(connection_id: str, key: str = WPS_KEY) -> str:
    digest = hmac.new(key.encode(), connection_id.encode(), hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class _FakeSession:
    """Minimal ``AsyncSession`` stand-in — webhook tests don't need a real DB.

    ``execute`` returns a result whose ``.scalar_one_or_none()`` is
    driven by ``device_row`` (set per test).
    """

    def __init__(self, device_row=None):
        self.device_row = device_row
        self.commits = 0
        self.added: list = []
        self.executed: list = []

    async def execute(self, stmt, *args, **kwargs):
        # Record UPDATE statements emitted by ``device_presence.mark_online``
        # / ``mark_offline`` so tests can assert the side effect without
        # needing a real DB.
        self.executed.append(stmt)
        # SELECT-style reads (the webhook's device lookup) return
        # ``device_row``; everything else returns ``None``.  The webhook's
        # only ``.scalar_one_or_none()`` caller is the device lookup.
        row = self.device_row
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=row)
        # Stage 4: mark_offline reads result.rowcount and compares to int
        # (> 0) to return a bool — make sure the mock returns an int.
        result.rowcount = 1
        return result

    def add(self, obj):
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, obj, *args, **kwargs):  # pragma: no cover - not hit here
        pass


@pytest_asyncio.fixture
async def app_and_session(monkeypatch):
    """Build a fresh FastAPI app containing only the wps_webhook router."""
    # Fresh DeviceManager for every test — isolation across webhook events.
    from cms.services import device_manager as dm_module
    dm_module.device_manager._connections.clear()

    # Stub Settings that satisfies the receiver's needs.
    class _S:
        wps_connection_string = CONN_STR
        wps_hub = "agora"
        wps_webhook_allowed_origin = None
        asset_base_url = None

    from cms.routers import wps_webhook as wh

    monkeypatch.setattr(wh, "get_settings", lambda: _S())

    session = _FakeSession()

    async def _fake_db():
        yield session

    from cms.database import get_db

    app = FastAPI()
    app.include_router(wh.router)
    app.dependency_overrides[get_db] = _fake_db

    yield app, session


@pytest_asyncio.fixture
async def client(app_and_session):
    app, _ = app_and_session
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


# ---------------------------------------------------------------- OPTIONS


@pytest.mark.asyncio
class TestAbuseProtectionHandshake:
    async def test_echoes_request_origin(self, client):
        r = await client.options(
            "/internal/wps/events",
            headers={"WebHook-Request-Origin": "foo.webpubsub.azure.com"},
        )
        assert r.status_code == 200
        assert r.headers["WebHook-Allowed-Origin"] == "foo.webpubsub.azure.com"

    async def test_wildcard_when_origin_unset(self, client):
        r = await client.options("/internal/wps/events")
        assert r.status_code == 200
        assert r.headers["WebHook-Allowed-Origin"] == "*"

    async def test_configured_origin_wins(self, monkeypatch, app_and_session, client):
        from cms.routers import wps_webhook as wh

        class _S:
            wps_connection_string = CONN_STR
            wps_hub = "agora"
            wps_webhook_allowed_origin = "only.me"
            asset_base_url = None

        monkeypatch.setattr(wh, "get_settings", lambda: _S())
        r = await client.options(
            "/internal/wps/events",
            headers={"WebHook-Request-Origin": "other.example"},
        )
        assert r.status_code == 200
        assert r.headers["WebHook-Allowed-Origin"] == "only.me"


# ---------------------------------------------------------------- signature


@pytest.mark.asyncio
class TestSignatureVerification:
    async def test_rejects_bad_signature(self, client):
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.connected",
                "ce-connectionId": "conn-1",
                "ce-userId": "pi-1",
                "ce-signature": "sha256=deadbeef",
            },
        )
        assert r.status_code == 401

    async def test_rejects_missing_signature(self, client):
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.connected",
                "ce-connectionId": "conn-1",
                "ce-userId": "pi-1",
            },
        )
        assert r.status_code == 401

    async def test_missing_connection_id_rejected(self, client):
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.connected",
                "ce-userId": "pi-1",
                "ce-signature": _sig("whatever"),
            },
        )
        assert r.status_code == 400


# ---------------------------------------------------------------- system events


@pytest.mark.asyncio
class TestSystemEvents:
    async def test_connected_marks_online_in_db(self, client, app_and_session):
        """Stage 2c: sys.connected must issue an UPDATE on devices.online."""
        _, session = app_and_session
        cid = "conn-register"
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.connected",
                "ce-connectionId": cid,
                "ce-userId": "pi-register",
                "ce-eventName": "connected",
                "ce-signature": _sig(cid),
            },
        )
        assert r.status_code == 204
        # mark_online → one UPDATE + one commit.
        assert getattr(session, "executed", []), "mark_online should have issued an UPDATE"
        assert session.commits >= 1
        # No ghost in the in-memory registry any more — presence is DB-only.
        from cms.services.device_manager import device_manager
        assert not device_manager.is_connected("pi-register")

    async def test_disconnected_marks_offline_in_db(self, client, app_and_session):
        """Stage 2c: sys.disconnected must issue an UPDATE on devices.online."""
        _, session = app_and_session
        cid = "old-cid"
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.disconnected",
                "ce-connectionId": cid,
                "ce-userId": "pi-gone",
                "ce-eventName": "disconnected",
                "ce-signature": _sig(cid),
            },
        )
        assert r.status_code == 204
        assert getattr(session, "executed", []), "mark_offline should have issued an UPDATE"
        assert session.commits >= 1


# ---------------------------------------------------------------- user events


@pytest.mark.asyncio
class TestUserEvents:
    async def test_unknown_device_is_logged_and_204(self, client, app_and_session):
        """Devices with no DB row are dropped with a warning.

        The connect-token endpoint that mints WPS URLs requires the
        device row to exist + have a valid X-Device-API-Key, so unknown
        devices reaching the webhook imply a race (row deleted mid-session)
        rather than a new-device bootstrap.
        """
        app, session = app_and_session
        session.device_row = None

        cid = "conn-unknown"
        r = await client.post(
            "/internal/wps/events",
            content=json.dumps({"type": "STATUS"}).encode(),
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.user.message",
                "ce-connectionId": cid,
                "ce-userId": "pi-unknown",
                "ce-eventName": "message",
                "ce-signature": _sig(cid),
            },
        )
        assert r.status_code == 204

    async def test_known_device_dispatches(self, client, app_and_session):
        """Valid CE on a user event routes to dispatch_device_message."""
        app, session = app_and_session
        # Fake device row with just enough attrs for InboundContext build.
        device = MagicMock()
        device.id = "pi-known"
        device.name = "Lobby"
        device.group_id = None
        device.status = MagicMock(value="adopted")
        session.device_row = device

        cid = "conn-known"
        payload = {"type": "STATUS", "mode": "play"}

        with patch(
            "cms.routers.wps_webhook.dispatch_device_message",
            new=AsyncMock(return_value=None),
        ) as mock_dispatch:
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps(payload).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-known",
                    "ce-eventName": "message",
                    "ce-signature": _sig(cid),
                },
            )
            assert r.status_code == 204
            mock_dispatch.assert_awaited_once()
            _, kwargs = mock_dispatch.call_args
            assert kwargs["msg"] == payload
            assert kwargs["ctx"].device_id == "pi-known"
            assert kwargs["ctx"].device is device
            # send closure should be wired to the current transport.
            assert callable(kwargs["send"])

    async def test_ce_time_header_parsed_into_received_at(
        self, client, app_and_session,
    ):
        """ce-time header (CloudEvents 1.0) is parsed into ctx.received_at.

        Temperature-alert dedupe relies on this monotonic timestamp;
        this test guards against regressions that drop the header.
        """
        from datetime import datetime, timezone

        app, session = app_and_session
        device = MagicMock()
        device.id = "pi-ce"
        device.name = "L"
        device.group_id = None
        device.status = MagicMock(value="adopted")
        session.device_row = device

        cid = "conn-ce"
        payload = {"type": "STATUS", "cpu_temp_c": 50.0}
        with patch(
            "cms.routers.wps_webhook.dispatch_device_message",
            new=AsyncMock(return_value=None),
        ) as mock_dispatch:
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps(payload).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-ce",
                    "ce-eventName": "message",
                    "ce-signature": _sig(cid),
                    "ce-time": "2026-04-23T06:30:00.123Z",
                },
            )
            assert r.status_code == 204
            _, kwargs = mock_dispatch.call_args
            received_at = kwargs["ctx"].received_at
            assert received_at is not None
            assert received_at == datetime(
                2026, 4, 23, 6, 30, 0, 123000, tzinfo=timezone.utc,
            )

    async def test_ce_time_missing_falls_back_to_none(
        self, client, app_and_session,
    ):
        """Missing ce-time header results in ctx.received_at=None.

        alert_service then falls back to server now() with a warning
        log.  Prevents a hard failure if Azure ever drops the header.
        """
        app, session = app_and_session
        device = MagicMock()
        device.id = "pi-no-time"
        device.name = "L"
        device.group_id = None
        device.status = MagicMock(value="adopted")
        session.device_row = device

        cid = "conn-no-time"
        payload = {"type": "STATUS"}
        with patch(
            "cms.routers.wps_webhook.dispatch_device_message",
            new=AsyncMock(return_value=None),
        ) as mock_dispatch:
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps(payload).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-no-time",
                    "ce-eventName": "message",
                    "ce-signature": _sig(cid),
                },
            )
            assert r.status_code == 204
            _, kwargs = mock_dispatch.call_args
            assert kwargs["ctx"].received_at is None

    async def test_ce_time_malformed_falls_back_to_none(
        self, client, app_and_session,
    ):
        """Malformed ce-time header is logged and ctx.received_at=None."""
        app, session = app_and_session
        device = MagicMock()
        device.id = "pi-bad-time"
        device.name = "L"
        device.group_id = None
        device.status = MagicMock(value="adopted")
        session.device_row = device

        cid = "conn-bad-time"
        payload = {"type": "STATUS"}
        with patch(
            "cms.routers.wps_webhook.dispatch_device_message",
            new=AsyncMock(return_value=None),
        ) as mock_dispatch:
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps(payload).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-bad-time",
                    "ce-eventName": "message",
                    "ce-signature": _sig(cid),
                    "ce-time": "totally not a timestamp",
                },
            )
            assert r.status_code == 204
            _, kwargs = mock_dispatch.call_args
            assert kwargs["ctx"].received_at is None

    async def test_non_json_body_is_400(self, client, app_and_session):
        _, session = app_and_session
        session.device_row = MagicMock(
            id="pi-known", name="x", group_id=None, status=MagicMock(value="adopted"),
        )
        cid = "conn-badbody"
        r = await client.post(
            "/internal/wps/events",
            content=b"not json at all",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.user.message",
                "ce-connectionId": cid,
                "ce-userId": "pi-known",
                "ce-eventName": "message",
                "ce-signature": _sig(cid),
            },
        )
        assert r.status_code == 400

    async def test_register_over_wps_calls_helper_and_pushes_auth(
        self, client, app_and_session,
    ):
        """A register message over WPS runs the shared known-device helper
        and pushes any newly-minted auth_assigned payload back to the
        device via the transport — it does NOT fall through to
        dispatch_device_message (register isn't a dispatcher event)."""
        app, session = app_and_session
        device = MagicMock()
        device.id = "pi-wps"
        device.name = "Kiosk"
        device.group_id = None
        device.status = MagicMock(value="adopted")
        session.device_row = device

        auth_payload = {"type": "auth_assigned", "device_auth_token": "new-token"}

        fake_transport = MagicMock()
        fake_transport.send_to_device = AsyncMock(return_value=True)

        cid = "conn-reg"
        payload = {
            "type": "register",
            "device_id": "pi-wps",
            "auth_token": "",
            "firmware_version": "1.11.7",
        }

        with patch(
            "cms.routers.wps_webhook.register_known_device",
            new=AsyncMock(return_value=MagicMock(
                orphaned=False, auth_assigned=auth_payload,
            )),
        ) as mock_reg, patch(
            "cms.routers.wps_webhook.dispatch_device_message",
            new=AsyncMock(return_value=None),
        ) as mock_dispatch, patch(
            "cms.routers.wps_webhook.get_transport",
            new=lambda: fake_transport,
        ):
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps(payload).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-wps",
                    "ce-eventName": "register",
                    "ce-signature": _sig(cid),
                },
            )

        assert r.status_code == 204
        mock_reg.assert_awaited_once()
        args, _ = mock_reg.call_args
        assert args[0] is device
        assert args[1] == payload
        fake_transport.send_to_device.assert_awaited_once_with("pi-wps", auth_payload)
        mock_dispatch.assert_not_called()

    async def test_register_over_wps_no_auth_token_no_push(
        self, client, app_and_session,
    ):
        """If the helper returns auth_assigned=None (token was valid),
        the transport is not hit."""
        app, session = app_and_session
        device = MagicMock(
            id="pi-wps", name="Kiosk", group_id=None,
            status=MagicMock(value="adopted"),
        )
        session.device_row = device

        fake_transport = MagicMock()
        fake_transport.send_to_device = AsyncMock(return_value=True)

        cid = "conn-reg-ok"
        with patch(
            "cms.routers.wps_webhook.register_known_device",
            new=AsyncMock(return_value=MagicMock(
                orphaned=False, auth_assigned=None,
            )),
        ), patch(
            "cms.routers.wps_webhook.get_transport",
            new=lambda: fake_transport,
        ):
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps({
                    "type": "register", "device_id": "pi-wps",
                    "auth_token": "valid-token",
                }).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-wps",
                    "ce-eventName": "register",
                    "ce-signature": _sig(cid),
                },
            )
        assert r.status_code == 204
        fake_transport.send_to_device.assert_not_called()

    async def test_register_over_wps_persists_ip_address(
        self, client, app_and_session,
    ):
        """``register`` user-message must persist ``ip_address`` to the
        device row.  ``sys.connected`` has no body so it can't carry
        the IP, and the webhook origin we'd see at the HTTP layer is
        Azure WPS's egress, not the device's LAN address.  This is a
        regression test for #436 — the LAN IP only landed in the DB
        on direct-WS deployments before this fix.
        """
        app, session = app_and_session
        device = MagicMock(
            id="pi-wps", name="Kiosk", group_id=None,
            status=MagicMock(value="adopted"),
        )
        session.device_row = device

        fake_transport = MagicMock()
        fake_transport.send_to_device = AsyncMock(return_value=True)

        cid = "conn-reg-ip"
        with patch(
            "cms.routers.wps_webhook.register_known_device",
            new=AsyncMock(return_value=MagicMock(
                orphaned=False, auth_assigned=None,
            )),
        ), patch(
            "cms.services.device_presence.mark_online",
            new=AsyncMock(),
        ) as mock_mark, patch(
            "cms.routers.wps_webhook.get_transport",
            new=lambda: fake_transport,
        ):
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps({
                    "type": "register", "device_id": "pi-wps",
                    "auth_token": "valid-token",
                    "ip_address": "192.168.1.53",
                }).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-wps",
                    "ce-eventName": "register",
                    "ce-signature": _sig(cid),
                },
            )
        assert r.status_code == 204
        mock_mark.assert_awaited_once()
        _, kwargs = mock_mark.call_args
        assert kwargs.get("ip_address") == "192.168.1.53"
        assert kwargs.get("connection_id") == cid

    async def test_register_over_wps_no_ip_address_skips_mark_online(
        self, client, app_and_session,
    ):
        """If the firmware omits ``ip_address`` (older builds) we don't
        call ``mark_online`` from the register handler — the
        ``sys.connected`` event already flipped presence and we don't
        want to overwrite a previously-known IP with ``None``."""
        app, session = app_and_session
        device = MagicMock(
            id="pi-wps", name="Kiosk", group_id=None,
            status=MagicMock(value="adopted"),
            firmware_version="1.11.20",
            upgrade_started_at=None,
        )
        session.device_row = device

        fake_transport = MagicMock()
        fake_transport.send_to_device = AsyncMock(return_value=True)

        cid = "conn-reg-noip"
        with patch(
            "cms.routers.wps_webhook.register_known_device",
            new=AsyncMock(return_value=MagicMock(
                orphaned=False, auth_assigned=None,
            )),
        ), patch(
            "cms.services.device_presence.mark_online",
            new=AsyncMock(),
        ) as mock_mark, patch(
            "cms.routers.wps_webhook.get_transport",
            new=lambda: fake_transport,
        ):
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps({
                    "type": "register", "device_id": "pi-wps",
                    "auth_token": "valid-token",
                }).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-wps",
                    "ce-eventName": "register",
                    "ce-signature": _sig(cid),
                },
            )
        assert r.status_code == 204
        mock_mark.assert_not_called()

    async def test_register_over_wps_clears_upgrade_claim_on_fw_change(
        self, client, app_and_session,
    ):
        """When register reports a different firmware than the device row
        held *and* an upgrade claim was set at register time, the WPS
        path must clear ``Device.upgrade_started_at`` via a CAS UPDATE.
        Otherwise the device is permanently stuck with
        ``is_upgrading=true`` and can't be upgraded again.  Mirrors the
        Stage-4 logic in ``cms/routers/ws.py:204-217``.
        """
        from datetime import datetime, timezone

        app, session = app_and_session
        prior_claim = datetime.now(timezone.utc)
        device = MagicMock(
            id="pi-wps", name="Kiosk", group_id=None,
            status=MagicMock(value="adopted"),
            firmware_version="1.11.7",
            upgrade_started_at=prior_claim,
        )
        session.device_row = device

        fake_transport = MagicMock()
        fake_transport.send_to_device = AsyncMock(return_value=True)

        cid = "conn-reg-fwchg"
        with patch(
            "cms.routers.wps_webhook.register_known_device",
            new=AsyncMock(return_value=MagicMock(
                orphaned=False, auth_assigned=None,
            )),
        ), patch(
            "cms.routers.wps_webhook.get_transport",
            new=lambda: fake_transport,
        ):
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps({
                    "type": "register", "device_id": "pi-wps",
                    "auth_token": "valid-token",
                    "firmware_version": "1.11.20",
                }).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-wps",
                    "ce-eventName": "register",
                    "ce-signature": _sig(cid),
                },
            )
        assert r.status_code == 204
        # Confirm an UPDATE statement was issued targeting upgrade_started_at.
        update_stmts = [
            str(s) for s in session.executed
            if "UPDATE" in str(s).upper() and "upgrade_started_at" in str(s)
        ]
        assert update_stmts, (
            f"Expected CAS UPDATE clearing upgrade_started_at, "
            f"got {[str(s) for s in session.executed]}"
        )

    async def test_register_over_wps_keeps_upgrade_claim_on_same_fw(
        self, client, app_and_session,
    ):
        """Reconnect during an in-flight upgrade (same firmware as
        before) must NOT clear ``upgrade_started_at`` — the upgrade
        hasn't actually completed yet.  The upgrade-endpoint TTL is
        the safety net that releases the claim if the device never
        boots into the new version.
        """
        from datetime import datetime, timezone

        app, session = app_and_session
        prior_claim = datetime.now(timezone.utc)
        device = MagicMock(
            id="pi-wps", name="Kiosk", group_id=None,
            status=MagicMock(value="adopted"),
            firmware_version="1.11.20",
            upgrade_started_at=prior_claim,
        )
        session.device_row = device

        fake_transport = MagicMock()
        fake_transport.send_to_device = AsyncMock(return_value=True)

        cid = "conn-reg-samefw"
        with patch(
            "cms.routers.wps_webhook.register_known_device",
            new=AsyncMock(return_value=MagicMock(
                orphaned=False, auth_assigned=None,
            )),
        ), patch(
            "cms.routers.wps_webhook.get_transport",
            new=lambda: fake_transport,
        ):
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps({
                    "type": "register", "device_id": "pi-wps",
                    "auth_token": "valid-token",
                    "firmware_version": "1.11.20",
                }).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-wps",
                    "ce-eventName": "register",
                    "ce-signature": _sig(cid),
                },
            )
        assert r.status_code == 204
        update_stmts = [
            str(s) for s in session.executed
            if "UPDATE" in str(s).upper() and "upgrade_started_at" in str(s)
        ]
        assert not update_stmts, (
            f"Did not expect upgrade_started_at UPDATE for same-fw register, "
            f"got {update_stmts}"
        )

    async def test_register_over_wps_orphaned_device(self, client, app_and_session):
        """Orphaned result returns 204 without pushing — direct-WS path
        would close 4004 but over WPS we can't close from here."""
        app, session = app_and_session
        device = MagicMock(
            id="pi-wps", name="Kiosk", group_id=None,
            status=MagicMock(value="orphaned"),
        )
        session.device_row = device

        fake_transport = MagicMock()
        fake_transport.send_to_device = AsyncMock(return_value=True)

        cid = "conn-orph"
        with patch(
            "cms.routers.wps_webhook.register_known_device",
            new=AsyncMock(return_value=MagicMock(
                orphaned=True, auth_assigned=None,
            )),
        ), patch(
            "cms.routers.wps_webhook.get_transport",
            new=lambda: fake_transport,
        ):
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps({
                    "type": "register", "device_id": "pi-wps",
                    "auth_token": "wrong-token",
                }).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-wps",
                    "ce-eventName": "register",
                    "ce-signature": _sig(cid),
                },
            )
        assert r.status_code == 204
        fake_transport.send_to_device.assert_not_called()


# ---------------------------------------------------------------- unknown type


@pytest.mark.asyncio
class TestUnknownEventType:
    async def test_400_on_unknown(self, client):
        cid = "conn-x"
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.somethingnew",
                "ce-connectionId": cid,
                "ce-userId": "pi-1",
                "ce-signature": _sig(cid),
            },
        )
        assert r.status_code == 400


# ---------------------------------------------------------------- alert_service hooks
#
# Regression guard for the gap discovered during PR #404 prod validation:
# the WPS webhook path never fired alert_service.device_{re,dis}connected,
# so every online/offline lifecycle event on WPS-only devices (which is
# now 100% of prod) silently skipped alert bookkeeping — zero
# ``device_events`` rows, zero notifications, zero
# ``device_alert_state`` updates.  These tests pin the contract.


@pytest.mark.asyncio
class TestAlertServiceHooks:
    async def test_register_success_fires_device_reconnected(
        self, client, app_and_session,
    ):
        """Happy path: a valid WPS register message triggers
        alert_service.device_reconnected so the ONLINE event is logged
        and any prior offline alert state is cleared."""
        _, session = app_and_session
        device = MagicMock(id="pi-recon", group_id="g-1", status=MagicMock(value="adopted"))
        device.name = "Lobby"
        session.device_row = device

        cid = "conn-recon"
        with patch(
            "cms.routers.wps_webhook.register_known_device",
            new=AsyncMock(return_value=MagicMock(
                orphaned=False, auth_assigned=None,
            )),
        ), patch(
            "cms.routers.wps_webhook.alert_service"
        ) as mock_alert:
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps({
                    "type": "register", "device_id": "pi-recon",
                    "auth_token": "valid",
                }).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-recon",
                    "ce-eventName": "register",
                    "ce-signature": _sig(cid),
                },
            )
        assert r.status_code == 204
        mock_alert.device_reconnected.assert_called_once()
        kwargs = mock_alert.device_reconnected.call_args.kwargs
        assert mock_alert.device_reconnected.call_args.args[0] == "pi-recon"
        assert kwargs["device_name"] == "Lobby"
        assert kwargs["group_id"] == "g-1"
        assert kwargs["status"] == "adopted"

    async def test_orphaned_register_does_not_fire_reconnect(
        self, client, app_and_session,
    ):
        """Orphaned devices must not emit an ONLINE alert — the
        register was rejected, not accepted."""
        _, session = app_and_session
        device = MagicMock(id="pi-orph", group_id=None, status=MagicMock(value="orphaned"))
        device.name = "Old"
        session.device_row = device

        cid = "conn-orph-alert"
        with patch(
            "cms.routers.wps_webhook.register_known_device",
            new=AsyncMock(return_value=MagicMock(
                orphaned=True, auth_assigned=None,
            )),
        ), patch(
            "cms.routers.wps_webhook.alert_service"
        ) as mock_alert:
            r = await client.post(
                "/internal/wps/events",
                content=json.dumps({
                    "type": "register", "device_id": "pi-orph",
                    "auth_token": "wrong",
                }).encode(),
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.user.message",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-orph",
                    "ce-eventName": "register",
                    "ce-signature": _sig(cid),
                },
            )
        assert r.status_code == 204
        mock_alert.device_reconnected.assert_not_called()

    async def test_disconnected_with_matching_conn_id_fires_alert(
        self, client, app_and_session,
    ):
        """sys.disconnected for the *current* connection loads the
        device + group and fires alert_service.device_disconnected."""
        _, session = app_and_session
        device = MagicMock(id="pi-dc", group_id="g-2", status=MagicMock(value="adopted"))
        device.name = "Kitchen"
        session.device_row = device

        cid = "cid-current"
        with patch(
            "cms.routers.wps_webhook.alert_service"
        ) as mock_alert:
            r = await client.post(
                "/internal/wps/events",
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.sys.disconnected",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-dc",
                    "ce-eventName": "disconnected",
                    "ce-signature": _sig(cid),
                },
            )
        assert r.status_code == 204
        mock_alert.device_disconnected.assert_called_once()
        assert mock_alert.device_disconnected.call_args.args[0] == "pi-dc"
        kwargs = mock_alert.device_disconnected.call_args.kwargs
        assert kwargs["device_name"] == "Kitchen"
        assert kwargs["group_id"] == "g-2"
        assert kwargs["status"] == "adopted"

    async def test_disconnected_stale_conn_id_suppressed(
        self, client, app_and_session,
    ):
        """If the stored connection_id has been replaced (the device
        already reconnected on another replica), mark_offline returns
        False and we must NOT fire a disconnect alert — the device
        isn't actually offline."""
        _, session = app_and_session
        device = MagicMock(id="pi-stale", group_id="g-3", status=MagicMock(value="adopted"))
        device.name = "Lobby"
        session.device_row = device

        # Force the guard to reject: mark_offline returns False when
        # rowcount == 0 (stored connection_id no longer matches).
        original_execute = session.execute

        async def _rowcount_zero(stmt, *a, **kw):
            result = await original_execute(stmt, *a, **kw)
            # Only the UPDATE from mark_offline is affected — SELECTs
            # coming after would also see rowcount=0, but our code
            # returns before issuing any SELECT when was_current is False.
            result.rowcount = 0
            return result

        session.execute = _rowcount_zero

        cid = "cid-stale"
        with patch(
            "cms.routers.wps_webhook.alert_service"
        ) as mock_alert:
            r = await client.post(
                "/internal/wps/events",
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.sys.disconnected",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-stale",
                    "ce-eventName": "disconnected",
                    "ce-signature": _sig(cid),
                },
            )
        assert r.status_code == 204
        mock_alert.device_disconnected.assert_not_called()

    async def test_disconnected_unknown_device_no_alert(
        self, client, app_and_session,
    ):
        """If the guard matches but the device row was deleted between
        register and disconnect, the SELECT returns None and we skip
        the alert path quietly — no crash, no alert, just 204."""
        _, session = app_and_session
        session.device_row = None  # no row to load

        cid = "cid-ghost"
        with patch(
            "cms.routers.wps_webhook.alert_service"
        ) as mock_alert:
            r = await client.post(
                "/internal/wps/events",
                content=b"{}",
                headers={
                    "content-type": "application/json",
                    "ce-type": "azure.webpubsub.sys.disconnected",
                    "ce-connectionId": cid,
                    "ce-userId": "pi-ghost",
                    "ce-eventName": "disconnected",
                    "ce-signature": _sig(cid),
                },
            )
        assert r.status_code == 204
        mock_alert.device_disconnected.assert_not_called()


# ---------------------------------------------------------------- multi-key


@pytest.mark.asyncio
class TestMultiKeyAcceptance:
    async def test_any_configured_key_verifies(self, monkeypatch, app_and_session, client):
        """Connection strings may carry primary+secondary AccessKey for rotation."""
        from cms.routers import wps_webhook as wh

        conn = f"Endpoint=http://b;AccessKey=new-key;AccessKey=old-key;Version=1.0;"

        class _S:
            wps_connection_string = conn
            wps_hub = "agora"
            wps_webhook_allowed_origin = None
            asset_base_url = None

        monkeypatch.setattr(wh, "get_settings", lambda: _S())

        cid = "conn-rot"
        # Sign with the OLD key — receiver should still accept.
        r = await client.post(
            "/internal/wps/events",
            content=b"{}",
            headers={
                "content-type": "application/json",
                "ce-type": "azure.webpubsub.sys.connected",
                "ce-connectionId": cid,
                "ce-userId": "pi-rot",
                "ce-signature": _sig(cid, key="old-key"),
            },
        )
        assert r.status_code == 204
