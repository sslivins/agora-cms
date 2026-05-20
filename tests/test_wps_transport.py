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
        # (user_id, body_dict, content_type=...). The dict is passed
        # straight through -- the Azure SDK serializes once when
        # content_type=application/json. Passing json.dumps(...) here
        # used to double-encode and crash the device transport.
        assert args[0] == "pi-1"
        assert args[1] == {"type": "ping"}
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

    async def test_404_dispatches_offline_alert_via_helper(self, monkeypatch):
        """Issue #406 — 404 on send must trigger the offline-alert path.

        We assert that ``mark_offline_and_alert`` is invoked with the
        snapshotted ``connection_id`` (the CAS token).  Don't go all
        the way to a real DB here — coverage for the helper's own
        contract lives in ``tests/test_device_presence.py``.
        """
        from azure.core.exceptions import HttpResponseError

        t, c, _ = _make_transport()
        err = HttpResponseError(message="not connected")
        err.status_code = 404
        c.send_to_user.side_effect = err

        # Stub the snapshot lookup to return a known cid.
        class _DummySession:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return None

            async def execute(self, _stmt):
                return SimpleNamespace(scalar_one_or_none=lambda: "cid-pre")

            async def commit(self):
                return None

        monkeypatch.setattr(
            "cms.services.wps_transport._session", lambda: _DummySession(),
        )

        called: list[dict] = []

        async def _fake_helper(db, did, *, expected_connection_id):
            called.append({"db": db, "did": did, "cid": expected_connection_id})
            return True

        monkeypatch.setattr(
            "cms.services.wps_transport.device_presence."
            "mark_offline_and_alert",
            _fake_helper,
        )

        ok = await t.send_to_device("pi-ghost", {"type": "ping"})
        assert ok is False
        assert len(called) == 1
        assert called[0]["did"] == "pi-ghost"
        assert called[0]["cid"] == "cid-pre"

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


@pytest.mark.asyncio
class TestSendToDeviceMetrics:
    """Verify cms.metrics counters are incremented on each branch.

    The metric registry is module-level so we monkeypatch the ``add``
    method of each counter with a Mock for the duration of the test.
    """

    @pytest.fixture
    def counters(self, monkeypatch):
        from cms import metrics as m

        attempt = MagicMock()
        success = MagicMock()
        failed = MagicMock()
        monkeypatch.setattr(m.wps_send_attempt_total, "add", attempt)
        monkeypatch.setattr(m.wps_send_success_total, "add", success)
        monkeypatch.setattr(m.wps_send_failed_total, "add", failed)
        return SimpleNamespace(
            attempt=attempt, success=success, failed=failed,
        )

    async def test_success_increments_attempt_and_success(self, counters):
        t, c, _ = _make_transport()
        await t.send_to_device("pi-1", {"type": "ping"})
        counters.attempt.assert_called_once_with(1)
        counters.success.assert_called_once_with(1)
        counters.failed.assert_not_called()

    async def test_404_increments_failure_with_404_reason(
        self, counters, monkeypatch,
    ):
        from azure.core.exceptions import HttpResponseError
        from cms import metrics as m

        t, c, _ = _make_transport()
        err = HttpResponseError(message="not connected")
        err.status_code = 404
        c.send_to_user.side_effect = err

        # Stub session + offline-alert helper so we don't reach the DB.
        class _DummySession:
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return None
            async def execute(self, _stmt):
                return SimpleNamespace(scalar_one_or_none=lambda: None)
            async def commit(self): return None

        monkeypatch.setattr(
            "cms.services.wps_transport._session", lambda: _DummySession(),
        )

        async def _noop(*a, **kw):
            return True

        monkeypatch.setattr(
            "cms.services.wps_transport.device_presence."
            "mark_offline_and_alert",
            _noop,
        )

        await t.send_to_device("pi-1", {"type": "ping"})
        counters.attempt.assert_called_once_with(1)
        counters.success.assert_not_called()
        counters.failed.assert_called_once_with(
            1, {m.ATTR_REASON: m.WPS_REASON_404},
        )

    async def test_429_increments_failure_with_429_reason(self, counters):
        from azure.core.exceptions import HttpResponseError
        from cms import metrics as m

        t, c, _ = _make_transport()
        err = HttpResponseError(message="throttled")
        err.status_code = 429
        c.send_to_user.side_effect = err

        await t.send_to_device("pi-1", {"type": "ping"})
        counters.failed.assert_called_once_with(
            1, {m.ATTR_REASON: m.WPS_REASON_429},
        )

    async def test_other_http_error_uses_http_error_reason(self, counters):
        from azure.core.exceptions import HttpResponseError
        from cms import metrics as m

        t, c, _ = _make_transport()
        err = HttpResponseError(message="boom")
        err.status_code = 500
        c.send_to_user.side_effect = err

        await t.send_to_device("pi-1", {"type": "ping"})
        counters.failed.assert_called_once_with(
            1, {m.ATTR_REASON: m.WPS_REASON_HTTP_ERROR},
        )

    async def test_unexpected_exception_uses_unexpected_reason(
        self, counters,
    ):
        from cms import metrics as m

        t, c, _ = _make_transport()
        c.send_to_user.side_effect = RuntimeError("network died")

        await t.send_to_device("pi-1", {"type": "ping"})
        counters.failed.assert_called_once_with(
            1, {m.ATTR_REASON: m.WPS_REASON_UNEXPECTED},
        )


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
class TestClose:
    async def test_close_propagates_to_client(self):
        t, c, _ = _make_transport()
        await t.close()
        c.close.assert_awaited_once()
