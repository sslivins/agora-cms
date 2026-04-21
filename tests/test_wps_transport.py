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
    # Stage 2c: WPS transport presence now reads from the ``devices``
    # table via ``device_presence`` helpers, which require a live session
    # factory.  Coverage for presence semantics lives in
    # ``tests/test_device_presence.py`` (pure helpers) and the transport
    # contract is exercised via the Local transport's contract tests.
    pass


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
    # Stage 2c: ``request_logs`` now checks connectivity via async
    # ``is_connected`` which hits the DB.  These unit tests bypassed the
    # app fixture and relied on the now-removed in-memory
    # ``register_remote`` path.  The contract is exercised through the
    # Local transport's ``test_request_logs_resolves_via_manager_hook``
    # in ``test_device_transport_contract.py`` (which uses a fresh DB).
    pass


@pytest.mark.asyncio
class TestClose:
    async def test_close_propagates_to_client(self):
        t, c, _ = _make_transport()
        await t.close()
        c.close.assert_awaited_once()
