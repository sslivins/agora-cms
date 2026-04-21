# `local-broker/` — Web-PubSub-shaped message broker (dev/CI)

This directory ships a small FastAPI service that implements the subset
of the Azure Web PubSub REST API and upstream webhook contract that CMS
needs, so we can run the full device ↔ CMS stack in `docker compose`
and CI without hitting a real Azure resource.

Part of the multi-replica rollout — see
[`docs/multi-replica-architecture.md`](../docs/multi-replica-architecture.md)
(issue #344). Stage 2a introduces this service standalone; Stage 2b
wires CMS up to it via `WPSTransport`.

## Why not the real thing in CI?

Microsoft does not ship an Azure Web PubSub emulator or edge container.
The F1 SKU (free, 20 concurrent connections) isn't viable for CI
because:

- Requires outbound net + service credentials in every job.
- Caps globally at 20 connections — scaling tests would trip it.
- We can't `docker compose kill` a replica we don't own. The multi-
  replica smoke test needs to scale CMS against a known-single broker
  to isolate "are CMS replicas stepping on each other?" from "is the
  broker doing something weird?".

This broker is 1 file and ~400 lines. Its contract is tiny because we
only implement what CMS uses. It is **not** a general Web PubSub
replacement.

## Contract

All endpoints match Azure Web PubSub verbatim where they exist.

### REST (CMS → broker)

```
POST  /api/hubs/{hub}/users/{userId}/:send
POST  /api/hubs/{hub}/users/{userId}/:closeConnections
GET   /health
GET   /_debug/users           (dev only — broker is on private net)
```

`send` body:

```json
{ "data": { ... }, "dataType": "json" }
```

Supported `dataType`: `json`, `text`, `binary` (all delivered as a WS
text frame today — Pi client doesn't speak binary).

Auth on every `/api/...` route: `Authorization: Bearer <jwt>` where the
JWT is HS256-signed with `WPS_ACCESS_KEY`. `aud` must start with the
full endpoint URI prefix (standard WPS server-JWT check).

### WSS (device → broker)

```
GET  /client/hubs/{hub}?access_token=<jwt>
```

Client JWT (HS256, same key): `sub` = device_id (→ WPS `userId`),
`exp` required. The broker validates, accepts the upgrade, and starts
relaying messages as webhook events.

### Upstream webhooks (broker → CMS)

CloudEvents 1.0 HTTP binary binding. POST to `WPS_UPSTREAM_URL`:

| header          | value                                                   |
|-----------------|---------------------------------------------------------|
| `ce-specversion`| `1.0`                                                   |
| `ce-type`       | `azure.webpubsub.sys.connected` / `.disconnected` / `azure.webpubsub.user.message` |
| `ce-source`     | `/hubs/{hub}`                                           |
| `ce-id`         | uuid                                                    |
| `ce-time`       | RFC3339                                                 |
| `ce-signature`  | `sha256=<hex HMAC-SHA256(body, WPS_ACCESS_KEY)>`        |

Body shape matches Azure docs. See `broker.py::_post_webhook` and tests
for ground truth.

## Running standalone

```bash
pip install -r requirements.txt
WPS_ACCESS_KEY=dev WPS_UPSTREAM_URL=http://localhost:8080/internal/wps/events \
  python -m uvicorn broker:app --port 7080
```

## Running under compose

Opt-in — compose ships a `wps` profile so the default `docker compose
up` doesn't pull this service in yet:

```bash
docker compose --profile wps up --build broker
```

Stage 2b will flip the default.

## Config

| env var                | default                            | description                                 |
|------------------------|------------------------------------|---------------------------------------------|
| `WPS_ACCESS_KEY`       | `dev-broker-access-key-change-me`  | HS256 secret — must match CMS config.       |
| `WPS_HUB`              | `agora`                            | Default hub.                                |
| `WPS_UPSTREAM_URL`     | (unset — webhooks dropped)         | CMS webhook endpoint.                       |
| `WPS_UPSTREAM_TIMEOUT_S`| `5.0`                             | Webhook POST timeout.                       |
| `WPS_PORT`             | `7080`                             | HTTP listen port.                           |

## Tests

```bash
cd local-broker
pip install -r requirements.txt
pip install pytest pytest-asyncio
pytest tests/ -v
```

Covers: REST auth, REST user-send, WSS connect/reject, webhook signing,
end-to-end loopback (client sends → webhook fires → REST send → client
receives).

## What's *not* implemented (and why)

- Groups, broadcast to hub → CMS doesn't use them yet. If/when Stage 5
  needs them, add `:send` on `/api/hubs/{hub}/groups/{group}` + join/leave.
- Permissions beyond user-scoped JWT → CMS mints per-device tokens that
  can only send to/receive as their own `sub`. Group roles out of scope.
- Persistence → broker is a cache; connections are memory-only.
- Abuse-protection handshake (`WebHook-Request-Origin` OPTIONS) →
  not used in compose; Stage 2b/prod can add it if prod upstream
  insists on it.

None of these are required for Stages 2–4; revisit in Stage 5 if the
generic command outbox needs them.
