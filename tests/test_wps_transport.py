"""Unit tests for ``cms.services.wps_transport.WPSTransport``.

The Azure SDK's ``WebPubSubServiceClient`` is mocked at import site; no
real WPS endpoint is contacted.  The transport shares presence state
with the in-process ``DeviceManager``, so we register ghost entries
(``register_remote``) to drive ``is_connected`` / state-read paths.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cms.services.device_manager import DeviceManager


def _make_transport(manager: DeviceManager | None = None):
    """Create a ``WPSTransport`` with the SDK client mocked out.

    Returns ``(transport, fake_client, manager)``.  ``fake_client``
    exposes the mocked ``send_to_user`` / ``get_client_access_token`` /
    ``close`` coroutines so tests can assert on them.
    """
    if manager is None:
        manager = DeviceManager()

    fake_client = MagicMock()
    fake_client.send_to_user = AsyncMock(return_value=None)
    fake_client.close = AsyncMock(return_value=None)
    fake_client.get_client_access_token = AsyncMock(
        return_value={
            "url": "wss://broker/client/hubs/agora?access_token=xyz",
            "baseUrl": "wss://broker/client/hubs/agora",
            "token": "xyz",
        }
    )

    with patch(
        "cms.services.wps_transport.WebPubSubServiceClient"
    ) as svc_cls:
        svc_cls.from_connection_string.return_value = fake_client
        from cms.services.wps_transport import WPSTransport  # noqa: WPS433
        t = WPSTransport(
            "Endpoint=http://broker:7080;AccessKey=k;Version=1.0;",
            hub="agora",
            device_manager=manager,
        )
    return t, fake_client, manager


@pytest.mark.asyncio
class TestSendToDevice:
    async def test_success_returns_true(self):
        t, c, _ = _make_transport()
        ok = await t.send_to_device("pi-1", {"type": "ping"})
        assert ok is True
        c.send_to_user.assert_awaited_once()
        args, kwargs = c.send_to_user.call_args
        # (user_id, body_str, content_type=...)
        assert args[0] == "pi-1"
        assert json.loads(args[1]) == {"type": "ping"}
        assert kwargs.get("content_type") == "application/json"

    async def test_404_returns_false(self):
        from azure.core.exceptions import HttpResponseError

        t, c, _ = _make_transport()

        # HttpResponseError with a .status_code attribute.
        err = HttpResponseError(message="not connected")
        err.status_code = 404
        c.send_to_user.side_effect = err

        ok = await t.send_to_device("pi-ghost", {"type": "ping"})
        assert ok is False

    async def test_other_http_error_returns_false(self):
        from azure.core.exceptions import HttpResponseError

        t, c, _ = _make_transport()
        err = HttpResponseError(message="boom")
        err.status_code = 500
        c.send_to_user.side_effect = err

        ok = await t.send_to_device("pi-1", {"type": "ping"})
        assert ok is False

    async def test_generic_exception_returns_false(self):
        t, c, _ = _make_transport()
        c.send_to_user.side_effect = RuntimeError("network died")
        ok = await t.send_to_device("pi-1", {"type": "ping"})
        assert ok is False


class TestPresenceDelegates:
    def test_empty_state(self):
        t, _, _ = _make_transport()
        assert t.connected_count == 0
        assert t.connected_ids == []
        assert not t.is_connected("pi-none")
        assert t.get_all_states() == []

    def test_reflects_register_remote(self):
        dm = DeviceManager()
        t, _, _ = _make_transport(manager=dm)
        dm.register_remote("pi-1", connection_id="cid-1", ip_address="10.0.0.1")
        dm.register_remote("pi-2", connection_id="cid-2")
        assert t.connected_count == 2
        assert t.is_connected("pi-1")
        assert set(t.connected_ids) == {"pi-1", "pi-2"}
        states_by_id = {s["device_id"]: s for s in t.get_all_states()}
        assert states_by_id["pi-1"]["ip_address"] == "10.0.0.1"

    def test_set_state_flags(self):
        dm = DeviceManager()
        t, _, _ = _make_transport(manager=dm)
        dm.register_remote("pi-1", connection_id="cid")
        t.set_state_flags("pi-1", ssh_enabled=True, local_api_enabled=False)
        s = {x["device_id"]: x for x in t.get_all_states()}["pi-1"]
        assert s["ssh_enabled"] is True
        assert s["local_api_enabled"] is False

    def test_set_state_flags_unknown_device_is_noop(self):
        t, _, _ = _make_transport()
        t.set_state_flags("ghost", ssh_enabled=True)  # must not raise


@pytest.mark.asyncio
class TestClientAccessToken:
    async def test_returns_dict_from_sdk(self):
        t, c, _ = _make_transport()
        out = await t.get_client_access_token("pi-1", minutes_to_expire=30)
        assert out["url"].startswith("wss://broker/")
        assert out["token"] == "xyz"
        c.get_client_access_token.assert_awaited_once()
        _, kwargs = c.get_client_access_token.call_args
        assert kwargs["user_id"] == "pi-1"
        assert kwargs["minutes_to_expire"] == 30


@pytest.mark.asyncio
class TestRequestLogs:
    async def test_raises_when_not_connected(self):
        t, _, _ = _make_transport()
        with pytest.raises(ValueError):
            await t.request_logs("pi-missing")

    async def test_resolves_via_manager_future(self):
        import asyncio

        dm = DeviceManager()
        t, c, _ = _make_transport(manager=dm)
        dm.register_remote("pi-1", connection_id="cid")

        sent_payloads: list[dict] = []

        async def _capture(user_id, body, **kwargs):
            sent_payloads.append(json.loads(body))

        c.send_to_user.side_effect = _capture

        task = asyncio.create_task(t.request_logs("pi-1", services=["agora"]))
        # Wait for send to hit the mock.
        for _ in range(100):
            if sent_payloads:
                break
            await asyncio.sleep(0.01)
        assert sent_payloads, "request_logs never dispatched"
        rid = sent_payloads[0]["request_id"]

        dm.resolve_log_request(rid, logs={"agora": "hello"})
        result = await asyncio.wait_for(task, timeout=1.0)
        assert result == {"agora": "hello"}


@pytest.mark.asyncio
class TestClose:
    async def test_close_propagates_to_client(self):
        t, c, _ = _make_transport()
        await t.close()
        c.close.assert_awaited_once()
