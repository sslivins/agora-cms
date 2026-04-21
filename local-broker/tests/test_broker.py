"""Unit tests for the local broker.

These tests import the app directly and drive it via ``httpx.ASGITransport``
(for REST) + a websockets client loopback for the WS path.  No external
services, no docker.
"""

from __future__ import annotations

import asyncio
import json
import os

import pytest

# Set env *before* importing broker so constants pick it up.
os.environ.setdefault("WPS_ACCESS_KEY", "test-broker-key")
os.environ.setdefault("WPS_UPSTREAM_URL", "")  # disable webhooks by default

import broker  # noqa: E402  - deliberately after env setup


@pytest.fixture(autouse=True)
def _reset_upstream(monkeypatch):
    """Each test starts with upstream unconfigured; opt in per-test."""
    monkeypatch.setattr(broker, "UPSTREAM_URL", "")
    yield


@pytest.fixture
def app_and_registry():
    app = broker.create_app()
    return app, app.state.registry


# ---------------------------------------------------------------- JWT helpers


class TestJwt:
    def test_mint_and_verify_server_token_roundtrip(self):
        token = broker.mint_server_token(audience="http://example/api/hubs/h")
        claims = broker._verify_jwt(token, expected_aud_prefix="http://example/api/hubs/h")  # gitleaks:allow
        assert claims["sub"] == "cms"
        assert claims["aud"].startswith("http://example/api/hubs/h")

    def test_mint_and_verify_client_token_roundtrip(self):
        token = broker.mint_client_token(user_id="pi-42")
        claims = broker._verify_jwt(token)
        assert claims["sub"] == "pi-42"

    def test_wrong_key_rejected(self):
        import jwt as _jwt
        token = broker.mint_client_token(user_id="pi-1", key="wrong-key")
        with pytest.raises(_jwt.InvalidTokenError):
            broker._verify_jwt(token)

    def test_expired_token_rejected(self):
        import jwt as _jwt
        token = broker.mint_client_token(user_id="pi-1", ttl_seconds=-10)
        with pytest.raises(_jwt.ExpiredSignatureError):
            broker._verify_jwt(token)

    def test_aud_prefix_mismatch_rejected(self):
        import jwt as _jwt
        token = broker.mint_server_token(audience="http://other/api/hubs/h")
        with pytest.raises(_jwt.InvalidTokenError):
            broker._verify_jwt(token, expected_aud_prefix="http://expected/api/hubs/h")  # gitleaks:allow


# ---------------------------------------------------------------- webhook sig


class TestWebhookSignature:
    def test_signature_roundtrip(self):
        cid = "conn-123"
        header = broker._sign_webhook(cid)
        assert header.startswith("sha256=")
        assert broker.verify_webhook_signature(cid, header)

    def test_signature_rejects_tampered_connection_id(self):
        header = broker._sign_webhook("conn-abc")
        assert not broker.verify_webhook_signature("conn-xyz", header)

    def test_signature_rejects_wrong_key(self):
        cid = "conn-1"
        header = broker._sign_webhook(cid)
        assert not broker.verify_webhook_signature(cid, header, keys=["other-key"])

    def test_signature_rejects_blank_header(self):
        assert not broker.verify_webhook_signature("conn-1", "")

    def test_multi_signature_rotation(self):
        """Two keys → two comma-separated sigs → valid if either matches."""
        cid = "conn-rot"
        header = broker._sign_webhook(cid, keys=["new-key", "old-key"])
        assert header.count("sha256=") == 2
        # Receiver holding only the new key verifies.
        assert broker.verify_webhook_signature(cid, header, keys=["new-key"])
        # Receiver holding only the old key verifies too.
        assert broker.verify_webhook_signature(cid, header, keys=["old-key"])
        # Receiver holding neither key rejects.
        assert not broker.verify_webhook_signature(cid, header, keys=["stranger"])


# ---------------------------------------------------------------- REST auth


def _client(app):
    import httpx
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.mark.asyncio
class TestRestAuth:
    async def test_missing_auth_header_rejected(self, app_and_registry):
        app, _ = app_and_registry
        async with _client(app) as c:
            r = await c.post(
                "/api/hubs/agora/users/pi-1/:send",
                json={"data": {"hello": "world"}, "dataType": "json"},
            )
            assert r.status_code == 401

    async def test_bad_bearer_token_rejected(self, app_and_registry):
        app, _ = app_and_registry
        async with _client(app) as c:
            r = await c.post(
                "/api/hubs/agora/users/pi-1/:send",
                json={"data": "x", "dataType": "text"},
                headers={"authorization": "Bearer not.a.jwt"},
            )
            assert r.status_code == 401

    async def test_send_to_unknown_user_404(self, app_and_registry):
        app, _ = app_and_registry
        aud = "http://testserver/api/hubs/agora/users/pi-ghost/"
        token = broker.mint_server_token(audience=aud)
        async with _client(app) as c:
            r = await c.post(
                "/api/hubs/agora/users/pi-ghost/:send",
                json={"data": "x", "dataType": "text"},
                headers={"authorization": f"Bearer {token}"},
            )
            assert r.status_code == 404


# ---------------------------------------------------------------- health


@pytest.mark.asyncio
class TestHealth:
    async def test_health_ok(self, app_and_registry):
        app, _ = app_and_registry
        async with _client(app) as c:
            r = await c.get("/health")
            assert r.status_code == 200
            body = r.json()
            assert body["ok"] is True
            assert body["connections"] == 0


# ---------------------------------------------------------------- WSS e2e


def _run_uvicorn_in_thread(app, port: int):
    """Spin up uvicorn in a background thread for a real-socket WSS test."""
    import threading
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)

    def _run():
        asyncio.run(server.serve())

    t = threading.Thread(target=_run, daemon=True)
    t.start()

    # Wait until the server is accepting connections.
    import socket
    import time
    deadline = time.time() + 5
    while time.time() < deadline:
        if server.started:
            break
        with socket.socket() as s:
            try:
                s.settimeout(0.1)
                s.connect(("127.0.0.1", port))
                break
            except OSError:
                time.sleep(0.05)
    return server


def _free_port() -> int:
    import socket
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.asyncio
class TestWssLoopback:
    async def test_reject_missing_token(self):
        app = broker.create_app()
        port = _free_port()
        server = _run_uvicorn_in_thread(app, port)
        try:
            import websockets
            with pytest.raises(Exception):
                async with websockets.connect(f"ws://127.0.0.1:{port}/client/hubs/agora"):
                    pass
        finally:
            server.should_exit = True
            await asyncio.sleep(0.2)

    async def test_reject_bad_token(self):
        app = broker.create_app()
        port = _free_port()
        server = _run_uvicorn_in_thread(app, port)
        try:
            import websockets
            with pytest.raises(Exception):
                async with websockets.connect(
                    f"ws://127.0.0.1:{port}/client/hubs/agora?access_token=garbage"
                ):
                    pass
        finally:
            server.should_exit = True
            await asyncio.sleep(0.2)

    async def test_full_loopback(self, monkeypatch):
        """Client connects → broker accepts → REST send delivers message to client."""
        app = broker.create_app()
        port = _free_port()
        server = _run_uvicorn_in_thread(app, port)
        try:
            import websockets

            token = broker.mint_client_token(user_id="pi-loop")
            uri = f"ws://127.0.0.1:{port}/client/hubs/agora?access_token={token}"

            async with websockets.connect(uri) as ws:
                # Give the broker a moment to register the connection.
                for _ in range(50):
                    await asyncio.sleep(0.02)
                    if app.state.registry.user_exists("pi-loop"):
                        break
                assert app.state.registry.user_exists("pi-loop")

                aud = f"http://127.0.0.1:{port}/api/hubs/agora/users/pi-loop/"
                server_token = broker.mint_server_token(audience=aud)

                async with _client_base(port) as c:
                    r = await c.post(
                        "/api/hubs/agora/users/pi-loop/:send",
                        json={"data": {"cmd": "PING"}, "dataType": "json"},
                        headers={"authorization": f"Bearer {server_token}"},
                    )
                    assert r.status_code == 202, r.text
                    assert r.json()["delivered"] == 1

                received = await asyncio.wait_for(ws.recv(), timeout=2.0)
                assert json.loads(received) == {"cmd": "PING"}
        finally:
            server.should_exit = True
            await asyncio.sleep(0.2)


def _client_base(port: int):
    import httpx
    return httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}")


# ---------------------------------------------------------------- webhook dispatch


@pytest.mark.asyncio
class TestUpstreamWebhooks:
    async def test_connected_and_disconnected_fire(self, monkeypatch):
        """Full loopback: connect, then close, assert webhooks arrive with
        valid sig and the CloudEvents 1.0 binary-binding header shape."""
        received: list[dict] = []

        async def _capture(request):
            import httpx
            body = await request.aread()
            event_type = request.headers.get("ce-type")
            cid = request.headers.get("ce-connectionid") or request.headers.get("ce-connectionId")
            sig = request.headers.get("ce-signature") or ""
            # Signature signs the connection id, not the body.
            assert broker.verify_webhook_signature(cid, sig), (
                f"bad sig on {event_type}: got {sig} cid={cid}"
            )
            received.append({
                "type": event_type,
                "headers": dict(request.headers),
                "body": body,
            })
            return httpx.Response(200)

        import httpx
        transport = httpx.MockTransport(_capture)

        app = broker.create_app()
        # Replace the shared httpx client with one routed through MockTransport.
        await app.state.http_client.aclose()
        app.state.http_client = httpx.AsyncClient(transport=transport)
        monkeypatch.setattr(broker, "UPSTREAM_URL", "http://cms/internal/wps/events")

        port = _free_port()
        server = _run_uvicorn_in_thread(app, port)
        try:
            import websockets
            token = broker.mint_client_token(user_id="pi-hook")
            uri = f"ws://127.0.0.1:{port}/client/hubs/agora?access_token={token}"
            async with websockets.connect(uri) as ws:
                await ws.send(json.dumps({"type": "HEARTBEAT"}))
                # Wait for connected + message to propagate.
                for _ in range(100):
                    await asyncio.sleep(0.05)
                    if len(received) >= 2:
                        break
            # Wait for disconnected webhook too.
            for _ in range(100):
                await asyncio.sleep(0.05)
                if any(r["type"].endswith(".disconnected") for r in received):
                    break
        finally:
            server.should_exit = True
            await asyncio.sleep(0.2)

        types = [r["type"] for r in received]
        assert "azure.webpubsub.sys.connected" in types
        assert "azure.webpubsub.user.message" in types
        assert "azure.webpubsub.sys.disconnected" in types

        # Header shape — every event carries ce-hub / ce-userId / ce-connectionId
        # / ce-source / ce-specversion.
        for ev in received:
            h = ev["headers"]
            assert h.get("ce-specversion") == "1.0"
            assert h.get("ce-hub") == "agora"
            assert h.get("ce-userid") == "pi-hook"
            cid = h.get("ce-connectionid")
            assert cid, "ce-connectionId missing"
            assert h.get("ce-source") == f"/hubs/agora/client/{cid}"

        # System events have empty-object body and short ce-eventName.
        for ev in received:
            if ev["type"].startswith("azure.webpubsub.sys."):
                assert ev["body"] == b"{}", f"sys event body must be empty obj, got {ev['body']!r}"
                assert ev["headers"].get("ce-eventname") in ("connected", "disconnected")

        # User message: raw client JSON bytes, ce-eventName = "message".
        msg_ev = next(r for r in received if r["type"].endswith("user.message"))
        assert msg_ev["headers"].get("ce-eventname") == "message"
        assert json.loads(msg_ev["body"]) == {"type": "HEARTBEAT"}
        assert msg_ev["headers"].get("content-type", "").startswith("application/json")
