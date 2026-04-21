"""Agora local broker — Web-PubSub-shaped message broker for dev/CI.

Microsoft does not ship an Azure Web PubSub emulator.  The prod deployment
will use real WPS; for local compose + CI we run this small FastAPI
service which implements the subset of the WPS REST surface and upstream
webhook contract that CMS actually uses.

Contract implemented:

REST (CMS → broker):
  POST  /api/hubs/{hub}/users/{userId}/:send
  POST  /api/hubs/{hub}/:closeUserConnections
  GET   /health

WSS (device → broker):
  GET   /client/hubs/{hub}?access_token=<jwt>
    JWT `sub` claim identifies the device (becomes the WPS `userId`).

Webhooks (broker → CMS) — Azure CloudEvents 1.0 binary binding:
  POST UPSTREAM_URL  with headers:
    ce-specversion:  1.0
    ce-type:         azure.webpubsub.sys.connected
                   | azure.webpubsub.sys.disconnected
                   | azure.webpubsub.user.<eventName>
    ce-source:       /hubs/{hub}/client/{connectionId}
    ce-id:           <unique>
    ce-time:         <RFC3339>
    ce-hub:          {hub}
    ce-connectionId: {connectionId}
    ce-userId:       {userId}
    ce-eventName:    {event_name}  (user events only; short form for
                                    system events: "connected" / "disconnected")
    ce-signature:    sha256=<hex HMAC_SHA256(access_key, connectionId)>
                     (comma-separated for key rotation; one entry today)
  Body:
    system events: b"{}"
    user events:   raw client data (application/json today)

Not implemented (intentionally — CMS does not use them):
  - Group operations
  - Broadcast to hub
  - Permissions / ACLs beyond user-scoped JWT
  - Connection state, custom protocols beyond raw JSON
  - `.connect` blocking event (our devices use JWT auth handled at WPS layer)

Security:
  - WSS: `access_token` JWT is HS256, signed with WPS_ACCESS_KEY.
    Claims required: `sub` (device_id / userId), `exp` (expiry).
  - REST: `Authorization: Bearer <server-jwt>` HS256, same key.
    `aud` claim must start with this broker's REST URI prefix.
  - Webhook: `ce-signature` = `hex(HMAC_SHA256(access_key, connectionId))`
    prefixed `sha256=`.  The signature covers the *connectionId*, not
    the body — matches Azure's contract.  CMS rejects on mismatch.

Environment variables:
  WPS_ACCESS_KEY            shared HS256 secret (required)
  WPS_HUB                   default hub name (default: "agora")
  WPS_UPSTREAM_URL          CMS webhook endpoint (required in compose/prod)
  WPS_UPSTREAM_TIMEOUT_S    webhook POST timeout (default: 5.0)
  WPS_PORT                  listen port (default: 7080)
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

import httpx
import jwt
from fastapi import (
    Body,
    FastAPI,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

logger = logging.getLogger("agora.broker")

# --------------------------------------------------------------------- config


def _getenv(name: str, default: str | None = None, required: bool = False) -> str:
    val = os.getenv(name, default)
    if required and not val:
        raise RuntimeError(f"{name} must be set")
    return val or ""


ACCESS_KEY = _getenv("WPS_ACCESS_KEY", "dev-broker-access-key-change-me")
DEFAULT_HUB = _getenv("WPS_HUB", "agora")
UPSTREAM_URL = _getenv("WPS_UPSTREAM_URL", "")
UPSTREAM_TIMEOUT_S = float(_getenv("WPS_UPSTREAM_TIMEOUT_S", "5.0"))

JWT_ALG = "HS256"


# ----------------------------------------------------------------- connections


@dataclass
class _Conn:
    connection_id: str
    user_id: str
    hub: str
    ws: WebSocket
    connected_at: float = field(default_factory=time.time)


class ConnectionRegistry:
    """In-memory connection tracker.

    The broker is single-replica by design (see multi-replica-architecture.md
    decision log) so a dict is sufficient.  A device may hold multiple
    simultaneous connections; sends fan-out to all of them.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, _Conn] = {}
        self._by_user: dict[str, set[str]] = {}
        self._lock = asyncio.Lock()

    async def add(self, conn: _Conn) -> None:
        async with self._lock:
            self._by_id[conn.connection_id] = conn
            self._by_user.setdefault(conn.user_id, set()).add(conn.connection_id)

    async def remove(self, connection_id: str) -> _Conn | None:
        async with self._lock:
            conn = self._by_id.pop(connection_id, None)
            if conn is None:
                return None
            ids = self._by_user.get(conn.user_id)
            if ids is not None:
                ids.discard(connection_id)
                if not ids:
                    self._by_user.pop(conn.user_id, None)
            return conn

    def connections_for_user(self, user_id: str) -> list[_Conn]:
        ids = self._by_user.get(user_id, set())
        return [self._by_id[cid] for cid in ids if cid in self._by_id]

    def user_exists(self, user_id: str) -> bool:
        return bool(self._by_user.get(user_id))

    @property
    def total(self) -> int:
        return len(self._by_id)

    @property
    def user_ids(self) -> list[str]:
        return list(self._by_user.keys())


# --------------------------------------------------------------------- auth


def _verify_jwt(token: str, *, expected_aud_prefix: str | None = None) -> dict[str, Any]:
    """Verify an HS256 JWT against ACCESS_KEY.  Returns claims on success.

    Raises ``jwt.InvalidTokenError`` subclasses on failure.
    """
    # We check `aud` manually below (prefix match, not exact) — disable
    # PyJWT's exact-audience verification.
    options = {"require": ["sub", "exp"], "verify_aud": False}
    claims = jwt.decode(
        token,
        ACCESS_KEY,
        algorithms=[JWT_ALG],
        options=options,
    )
    if expected_aud_prefix:
        aud = claims.get("aud")
        if not isinstance(aud, str) or not aud.startswith(expected_aud_prefix):
            raise jwt.InvalidTokenError("aud mismatch")
    return claims


def _sign_webhook(connection_id: str, keys: list[str] | None = None) -> str:
    """Return the ``ce-signature`` header value.

    Signs the ``connection_id`` (not the body) — matches Azure's
    contract.  ``keys`` is a list to allow primary+secondary signatures
    for rotation; today the broker has exactly one key, so callers pass
    ``[ACCESS_KEY]`` and a single ``sha256=<hex>`` entry is emitted.
    """
    key_list = keys if keys is not None else [ACCESS_KEY]
    parts: list[str] = []
    payload = connection_id.encode("utf-8")
    for k in key_list:
        digest = hmac.new(k.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        parts.append(f"sha256={digest}")
    return ",".join(parts)


def verify_webhook_signature(
    connection_id: str,
    header_value: str,
    keys: list[str] | None = None,
) -> bool:
    """Helper re-used by the broker's tests and the CMS receiver.

    A valid ``header_value`` is a comma-separated list of
    ``sha256=<hex>`` entries; the request is accepted if any entry
    matches ``hex(HMAC_SHA256(k, connection_id))`` for any ``k`` in
    ``keys``.
    """
    if not header_value:
        return False
    key_list = keys if keys is not None else [ACCESS_KEY]
    presented: list[str] = []
    for entry in header_value.split(","):
        entry = entry.strip()
        if not entry.lower().startswith("sha256="):
            continue
        presented.append(entry[len("sha256="):])
    if not presented:
        return False
    payload = connection_id.encode("utf-8")
    for k in key_list:
        want = hmac.new(k.encode("utf-8"), payload, hashlib.sha256).hexdigest()
        for got in presented:
            if hmac.compare_digest(want, got):
                return True
    return False


# --------------------------------------------------------------------- webhook


async def _post_webhook(
    client: httpx.AsyncClient,
    *,
    event_type: str,
    hub: str,
    connection_id: str,
    user_id: str,
    event_name: str | None = None,
    raw_body: bytes = b"{}",
    content_type: str = "application/json",
) -> None:
    """Post a CloudEvents 1.0 binary-binding webhook to the CMS.

    - System events (``azure.webpubsub.sys.connected`` /
      ``.disconnected``): ``raw_body`` is ``b"{}"``; ``event_name`` is
      the short form (``"connected"`` / ``"disconnected"``).
    - User events (``azure.webpubsub.user.<name>``): ``raw_body`` is the
      raw client payload bytes; ``event_name`` is the custom event name
      (e.g. ``"message"``).

    The ``ce-signature`` header signs ``connection_id``, not the body —
    matches Azure's upstream webhook contract.
    """
    if not UPSTREAM_URL:
        logger.debug("WPS_UPSTREAM_URL not set; dropping %s event", event_type)
        return
    headers = {
        "content-type": content_type,
        "ce-specversion": "1.0",
        "ce-type": event_type,
        "ce-source": f"/hubs/{hub}/client/{connection_id}",
        "ce-id": str(uuid.uuid4()),
        "ce-time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        "ce-hub": hub,
        "ce-connectionId": connection_id,
        "ce-userId": user_id,
        "ce-signature": _sign_webhook(connection_id),
    }
    if event_name:
        headers["ce-eventName"] = event_name
    try:
        r = await client.post(UPSTREAM_URL, content=raw_body, headers=headers, timeout=UPSTREAM_TIMEOUT_S)
        if r.status_code >= 400:
            logger.warning(
                "Upstream webhook %s -> %s failed: %s",
                event_type, UPSTREAM_URL, r.status_code,
            )
    except httpx.HTTPError:
        logger.exception("Upstream webhook %s -> %s errored", event_type, UPSTREAM_URL)


# --------------------------------------------------------------------- app


class SendPayload(BaseModel):
    """WPS user-send body.

    Matches the Azure REST shape:
        {"data": ..., "dataType": "json"|"text"}
    """

    data: Any
    dataType: str = Field(default="json", pattern="^(json|text|binary)$")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Agora local broker",
        description="Web-PubSub-shaped message broker for dev/CI.",
    )
    registry = ConnectionRegistry()
    app.state.registry = registry
    # Single async client — reused across webhook posts.
    app.state.http_client = httpx.AsyncClient()

    @app.on_event("shutdown")
    async def _shutdown() -> None:  # pragma: no cover - lifecycle
        await app.state.http_client.aclose()

    # ---------------- Health + introspection --------------

    @app.get("/health")
    async def health() -> dict[str, Any]:
        return {
            "ok": True,
            "connections": registry.total,
            "users": len(registry.user_ids),
            "upstream_configured": bool(UPSTREAM_URL),
        }

    @app.get("/_debug/users")
    async def debug_users() -> dict[str, Any]:
        """Unauthenticated: broker is dev-only, runs on private docker net."""
        return {"users": registry.user_ids}

    # ---------------- REST: send to user ------------------

    def _require_bearer(
        authorization: str | None,
        *,
        aud_prefix: str,
    ) -> dict[str, Any]:
        if not authorization or not authorization.lower().startswith("bearer "):
            raise HTTPException(status_code=401, detail="missing bearer token")
        token = authorization.split(" ", 1)[1].strip()
        try:
            return _verify_jwt(token, expected_aud_prefix=aud_prefix)
        except jwt.InvalidTokenError as e:
            raise HTTPException(status_code=401, detail=f"invalid token: {e}") from e

    @app.post("/api/hubs/{hub}/users/{user_id}/:send")
    async def send_to_user(
        hub: str,
        user_id: str,
        request: Request,
        payload: SendPayload = Body(...),
        authorization: str | None = Header(default=None),
    ) -> JSONResponse:
        # WPS server-JWT audience is the REST URI root for the hub.
        _require_bearer(
            authorization,
            aud_prefix=str(request.url.replace(query="", fragment="")).split(":send", 1)[0],
        )

        conns = registry.connections_for_user(user_id)
        if not conns:
            # WPS returns 404 when user has no active connections.
            return JSONResponse(status_code=404, content={"error": "user not connected"})

        # Shape we hand to the client follows WPS dataType semantics.
        if payload.dataType == "json":
            # WS text frame with JSON.  If data is already a dict, serialize.
            text = json.dumps(payload.data) if not isinstance(payload.data, str) else payload.data
            send_text = True
        elif payload.dataType == "text":
            text = payload.data if isinstance(payload.data, str) else json.dumps(payload.data)
            send_text = True
        else:  # binary
            # Devices today only speak JSON; accept but coerce for parity.
            text = payload.data if isinstance(payload.data, str) else json.dumps(payload.data)
            send_text = True

        delivered = 0
        for conn in conns:
            try:
                if send_text:
                    await conn.ws.send_text(text)
                delivered += 1
            except Exception:
                logger.exception(
                    "Failed to deliver to connection %s (user=%s)",
                    conn.connection_id, user_id,
                )
        return JSONResponse(status_code=202, content={"delivered": delivered})

    @app.post("/api/hubs/{hub}/users/{user_id}/:closeConnections")
    async def close_user_connections(
        hub: str,
        user_id: str,
        request: Request,
        authorization: str | None = Header(default=None),
    ) -> dict[str, Any]:
        _require_bearer(
            authorization,
            aud_prefix=str(request.url.replace(query="", fragment="")).split(":closeConnections", 1)[0],
        )
        closed = 0
        for conn in registry.connections_for_user(user_id):
            try:
                await conn.ws.close(code=1000)
                closed += 1
            except Exception:
                logger.exception("Failed to close conn %s", conn.connection_id)
        return {"closed": closed}

    # ---------------- WSS: device client ------------------

    @app.websocket("/client/hubs/{hub}")
    async def client_socket(
        websocket: WebSocket,
        hub: str,
        access_token: str = Query(default=""),
    ) -> None:
        # Validate JWT before accepting — WPS behaviour on bad token is 401.
        if not access_token:
            await websocket.close(code=4401)  # protocol-ish; fine for compose
            return
        try:
            claims = _verify_jwt(access_token)
        except jwt.InvalidTokenError:
            logger.info("Rejected client connection: invalid token")
            await websocket.close(code=4401)
            return

        user_id = str(claims.get("sub") or "")
        if not user_id:
            await websocket.close(code=4401)
            return

        connection_id = uuid.uuid4().hex
        await websocket.accept()
        conn = _Conn(
            connection_id=connection_id,
            user_id=user_id,
            hub=hub,
            ws=websocket,
        )
        await registry.add(conn)

        http_client: httpx.AsyncClient = app.state.http_client

        # Fire connected webhook.
        asyncio.create_task(
            _post_webhook(
                http_client,
                event_type="azure.webpubsub.sys.connected",
                hub=hub,
                connection_id=connection_id,
                user_id=user_id,
                event_name="connected",
                raw_body=b"{}",
            )
        )

        try:
            while True:
                msg = await websocket.receive()
                if msg["type"] == "websocket.disconnect":
                    break
                if "text" in msg and msg["text"] is not None:
                    text = msg["text"]
                    asyncio.create_task(
                        _post_webhook(
                            http_client,
                            event_type="azure.webpubsub.user.message",
                            hub=hub,
                            connection_id=connection_id,
                            user_id=user_id,
                            event_name="message",
                            raw_body=text.encode("utf-8"),
                            content_type="application/json",
                        )
                    )
                elif "bytes" in msg and msg["bytes"] is not None:
                    # Binary frames — not used by current Pi agent but honour the shape.
                    asyncio.create_task(
                        _post_webhook(
                            http_client,
                            event_type="azure.webpubsub.user.message",
                            hub=hub,
                            connection_id=connection_id,
                            user_id=user_id,
                            event_name="message",
                            raw_body=msg["bytes"],
                            content_type="application/octet-stream",
                        )
                    )
        except WebSocketDisconnect:
            pass
        except Exception:
            logger.exception("Unexpected error on client socket %s", connection_id)
        finally:
            await registry.remove(connection_id)
            # Fire disconnected webhook (fire-and-forget — no await on teardown).
            asyncio.create_task(
                _post_webhook(
                    http_client,
                    event_type="azure.webpubsub.sys.disconnected",
                    hub=hub,
                    connection_id=connection_id,
                    user_id=user_id,
                    event_name="disconnected",
                    raw_body=b"{}",
                )
            )

    return app


app = create_app()


# --------------------------------------------------------------------- utils


def mint_server_token(
    *, audience: str, ttl_seconds: int = 60, key: str | None = None
) -> str:
    """Convenience: mint a REST-server JWT (used by CMS + tests)."""
    now = int(time.time())
    return jwt.encode(
        {
            "aud": audience,
            "iat": now,
            "nbf": now,
            "exp": now + ttl_seconds,
            "sub": "cms",
        },
        key or ACCESS_KEY,
        algorithm=JWT_ALG,
    )


def mint_client_token(
    *, user_id: str, ttl_seconds: int = 3600, key: str | None = None,
    extra_claims: dict[str, Any] | None = None,
) -> str:
    """Convenience: mint a device WSS client JWT (used by CMS + tests)."""
    now = int(time.time())
    claims: dict[str, Any] = {
        "sub": user_id,
        "iat": now,
        "nbf": now,
        "exp": now + ttl_seconds,
    }
    if extra_claims:
        claims.update(extra_claims)
    return jwt.encode(claims, key or ACCESS_KEY, algorithm=JWT_ALG)


if __name__ == "__main__":  # pragma: no cover
    import uvicorn

    port = int(os.getenv("WPS_PORT", "7080"))
    uvicorn.run(app, host="0.0.0.0", port=port, log_level="info")
