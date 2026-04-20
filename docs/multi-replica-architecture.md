# Multi-replica CMS — architecture plan

Tracks issue #344. This plan supersedes the A/B/C analysis in the issue
comment; it picks a fourth option (managed WSS pub/sub) and lays out a
staged migration. The key open questions have been worked through —
see the Decision log at the bottom for rationale.

## Problem

`sslivins/agora-cms` is configured for up to 3 replicas in prod but is
not multi-replica safe. Shared in-memory state (`device_manager`,
scheduler caches, `_upgrading` guard, `_log_buffer`, version checker,
UI cache-bust, 9 singleton background loops) means a second replica
either silently drops device traffic or double-schedules work. This is
the immediate cause of the PR-244 / hot-fix class of bugs. Goal: CMS
is safe to scale horizontally, with smoke tests that prove it.

## Direction (locked after discussion)

1. **Prod transport:** Azure Web PubSub. Pis connect WSS directly to
   the managed service, CMS never terminates a device WebSocket. CMS
   sends via REST API, receives via upstream webhook. Presence,
   reconnect, backoff all become Azure's problem. Starts on F1 (free,
   20 concurrent connections) — upgrade to S1 ($48/mo, 1k connections)
   when the fleet outgrows F1.
2. **Local transport:** a small in-house broker container that
   implements the same subset of the Web PubSub REST + upstream
   webhook contract. Runs as a single replica in compose (no Redis,
   in-memory state). Reason for building it: Microsoft does not ship
   a Web PubSub emulator/edge container; F1 isn't viable for smoke
   tests because we can't `docker compose kill` a broker replica we
   don't own. The smoke tests scale CMS replicas against a single
   broker replica — that's where the bugs live, and one broker is
   enough to validate cross-replica CMS behaviour.
3. **One CMS transport implementation.** The broker presents the same
   contract as Web PubSub, so CMS code has a single transport class
   (endpoint + key env-driven). No `if env == 'azure'` branch in
   business logic. Vendor lock-in mitigated by the interface being
   ours — if we ever leave Azure, we write a new broker-side adapter,
   CMS unchanged.
4. **Postgres is the sole shared state for CMS.** Device telemetry
   (cpu_temp_c, load_avg, last_seen, mode, …) persists on every
   STATUS webhook. No in-memory `_states` dict. The UI polls
   `/api/devices` every 5 s (verified in `cms/templates/devices.html`)
   so every replica can serve the read directly from the DB and
   replicas converge within one poll interval. No cross-replica
   push fan-out needed.
5. **Device log RPC replaced by blob-upload workflow, CMS-proxied.**
   The Pi gzips its journal and `POST`s it to any CMS replica; CMS
   streams it into Azure Blob (Azurite in compose) and updates a
   `log_requests` row with the URL. Measured payload: ~92 KB gzipped
   for 7 days on a test Pi — no need for SAS-direct uploads. The
   `log_requests` table doubles as a transactional outbox
   (queued → sent → ready → expired/failed → reaped).
6. **Background loops: per-loop coordination, not uniform locks.**
   Stage 1 will audit every loop in `cms/main.py` and classify each:
   - *Leader-only* (`pg_try_advisory_lock`): scheduler tick, version
     checker, deleted-asset reaper, stream-capture monitor, log
     reaper, service-key rotation. One replica at a time.
   - *SKIP LOCKED drainer* (every replica): outbox drainers
     (`asset_transcode_queue`, `log_requests`, any future outbox).
     More replicas = faster drain.
   - *Replicated* (no coordination): in-process cache warmers
     (setup cache, alert refresh). Each replica maintains its own.
7. **At-least-once delivery is the assumption.** STATUS is idempotent
   by design; writes use a monotonic guard
   (`WHERE last_status_ts < :new_ts`) to defeat out-of-order retries.
   We intentionally do **not** add a `processed_events` dedup table —
   all current message types (STATUS, HEARTBEAT, LOG_RESPONSE,
   METRICS) are idempotent by construction. Revisit if a
   non-idempotent handler is introduced.
8. **Async UI for logs.** `POST /api/devices/{id}/logs` returns a
   `request_id` immediately; UI polls a status endpoint that any
   replica can serve from `log_requests`.
9. **Device authentication: minimum viable, hardening deferred.** The
   current direct-WS handshake is weak — anyone can open a socket
   and claim any `device_id`, and a successful impersonation rotates
   the API key to the attacker. SD cards are unencrypted too, so a
   physical attack trivially extracts on-device credentials. Rather
   than over-invest in the auth layer while storage is the weaker
   link, we:
   - Mint WPS JWTs scoped to a single per-device group
     (`sendToGroup:device-{id}`) so a leaked token can only talk
     about one device.
   - Enforce webhook HMAC signatures on the WPS → CMS ingress (new
     public attack surface, non-negotiable from day one).
   - Invest the hardening budget in **blast-radius reduction**: strict
     Pydantic schemas on every inbound message (`extra="forbid"`),
     per-message size caps, per-device rate limits, exception-
     swallowing handlers, path-traversal guards on device-supplied
     strings, `sqlalchemy.text()` audit.
   - Document SD-card encryption and stronger device-credential
     transport (e.g. per-device keypair enrolment) as known deferred
     items in `SECURITY.md`.
10. **Flag-day rollout.** Small internal fleet, we control firmware
    updates. Coordinated window: push Pi firmware + CMS cut-over the
    same day. No dual-endpoint (legacy `/ws/device` + WPS webhook)
    maintenance period.
11. **Observability via OpenTelemetry → App Insights.** OTel SDK in
    FastAPI, OTLP exporter, App Insights as the backend. Metrics,
    traces, and logs unified. Keeps the door open for alternate
    backends (Honeycomb, Grafana Cloud) with a config change.
12. **Blob storage: reuse the existing Azure storage account.** New
    container `device-logs` alongside the assets container. CMS
    authenticates via managed identity. Azurite for compose/dev.
    30-day auto-delete lifecycle rule on `device-logs`.

## Non-goals (for this effort)

- Migrating every CMS→device command into an outbox. Logs first; the
  generic `device_command_outbox` is a Stage 5 follow-up.
- Rewriting the Pi-side architecture. Pi keeps raw WS + JSON; only
  the URL, token-fetch flow, and log-upload path change.
- Any change to asset/transcode pipeline. It's already multi-replica
  safe (worker pinned to 1, outbox-driven).
- Writing a full Web PubSub emulator. Our broker implements only the
  subset of the REST surface + webhook contract that our transport
  uses.
- Strong device authentication (per-device keypair enrolment). Listed
  as a deferred item in `SECURITY.md`. Gated on SD-card encryption.

## Staged delivery plan

Each stage is independently shippable and leaves the system working.

### Stage 0 — Immediate mitigation

Pin CMS `maxReplicas: 1` in `infra/modules/containerApps.bicep`,
referencing issue #344. Independent of everything else, closes the
acute prod risk while the rest of the work lands. Worker stays at
1 replica (already correct). Single tiny PR.

### Stage 1 — Transport abstraction + loop classification

No infra change yet. Two things land together:

- Introduce a `DeviceTransport` interface inside CMS and wrap the
  existing in-process WS (`device_manager`) behind it. All call sites
  in `routers/devices.py`, `scheduler.py`, etc. stop calling
  `device_manager.send_to_device` directly and call the transport
  instead. No behaviour change.
- Audit every singleton loop in `cms/main.py`, classify as leader-
  only / SKIP LOCKED drainer / replicated, and annotate in code with
  a comment + the advisory lock ID it will use. No behavioural change
  yet — we're just setting the classification in stone so Stage 4 is
  pure follow-through.

Deliverables:
- `DeviceTransport` abstract class + local (in-process) implementation.
- All existing call sites migrated.
- Per-loop classification table in this doc + code comments.
- Tests updated, no functional change.

### Stage 2 — Local broker + WPS transport + hardening

Ship the broker container and the WPS-shaped transport implementation
in CMS. Compose uses the broker by default. Security hardening of the
message surface lands here too, since we're already touching every
inbound code path.

Deliverables:
- `local-broker/` new service. Single replica, in-memory state, no
  Redis. Implements the WPS REST subset + upstream webhook contract
  we actually use.
- `WPSTransport` implementation in CMS, selected by `DEVICE_TRANSPORT=wps`
  env. Broker URL + shared secret configured.
- Webhook handler in CMS (`/internal/wps/events`) with HMAC signature
  validation (constant-time compare).
- JWT issuance endpoint `POST /api/devices/{id}/connect-token`. Scoped
  to `sendToGroup:device-{id}` only.
- Device telemetry persistence (`devices.cpu_temp_c` etc. column
  additions + UPDATE on webhook). HOT-friendly: `fillfactor = 80` via
  `ALTER TABLE` (no `VACUUM FULL` — applies to new pages; existing
  small table is rewritten organically).
- Monotonic guard on STATUS UPDATEs (`WHERE last_status_ts < :new_ts`).
- **Message-surface hardening audit**:
  - Strict Pydantic schemas on every inbound type (`extra="forbid"`,
    explicit types).
  - Per-message size caps (STATUS ≤ 8 KB, log bundles ≤ 10 MB).
  - Per-device rate limits on inbound webhook (STATUS ≤ 1 / 10 s).
  - Exception-swallowing handlers — never echo request content or
    stack traces.
  - Path-traversal guards on device-supplied strings.
  - `sqlalchemy.text()` usage audit.
- OpenTelemetry instrumentation bootstrap + App Insights exporter.
- Integration tests for the full path.

Deliberately still single CMS replica. We can stop here, point prod at
Azure Web PubSub F1 in a limited rollout, and never enable N>1 if we
get cold feet. Everything from Stage 2 onward is incremental.

### Stage 3 — Logs via blob outbox (CMS-proxied)

Replace the synchronous REQUEST_LOGS / LOGS_RESPONSE RPC with the
blob-upload workflow. Log data no longer travels on the realtime
channel.

Deliverables:
- `log_requests` table with outbox columns.
- Drainer loop on CMS (every replica, `SKIP LOCKED`) sends REQUEST_LOGS
  via transport, retries with backoff.
- Pi side: on REQUEST_LOGS, Pi gzips its journal (verified ≈92 KB for
  7 days on a test Pi) and `POST`s it to
  `/api/devices/{id}/logs/{request_id}/upload` on any CMS replica.
  CMS streams the body into Azure Blob (or Azurite) using managed
  identity, writes the blob URL to `log_requests.blob_url`, flips
  status to `ready`.
- Reaper: expire stale `sent` rows, delete old blobs per the 30-day
  lifecycle rule, orphan-blob sweep.
- Async UI: `request_id` returned immediately, status-poll endpoint
  (`GET /api/logs/{request_id}`).
- Old REQUEST_LOGS / LOGS_RESPONSE code paths removed from CMS +
  `_pending_log_requests` map deleted.

At this point the CMS is fully stateless for log RPCs. Cross-replica
safety for logs is proven by design.

### Stage 4 — Enable N>1 replicas in prod + smoke tests

Everything above has been merged but prod still runs at 1 replica.
This stage enables horizontal scale and proves it.

Deliverables:
- Apply the loop classification from Stage 1:
  - **Leader-only** loops wrapped in `pg_try_advisory_lock`.
  - **SKIP LOCKED** drainers start on every replica.
  - **Replicated** cache warmers start on every replica unchanged.
- `_log_buffer` (CMS's own log deque) removed in favour of App
  Insights query — OTel instrumentation from Stage 2 already ships
  the equivalent data.
- `_upgrading` set in devices router moved into a Postgres table
  (short TTL).
- Cross-replica RPC correlation for any commands that still do
  request/response (after Stage 3, logs no longer needs it). Only
  if such commands exist — audit during this stage.
- Smoke test compose profile: `--scale cms=2`, single broker, single
  pi-simulator-swarm. Scenarios documented below.
- Lift `maxReplicas` back to 3 in bicep.

**Smoke tests at this stage:**
- Cross-replica send: Pi connected via broker, command issued from
  cms-2, verify delivery via cms-1 or cms-2.
- Cross-replica inbound: Pi STATUS handled by either CMS replica;
  telemetry visible via `/api/devices` from the other.
- Cross-replica log RPC: full blob workflow across replicas
  (drainer on cms-1 fires REQUEST_LOGS; upload handled by cms-2;
  UI status poll served by cms-1).
- Broker failure: kill the broker mid-session, Pi reconnect logic
  engages, subsequent commands deliver once the broker is restarted.
- CMS replica failure: kill leader mid-scheduler-tick, standby
  takes advisory lock within N seconds, no schedule duplication.
- Load: 50 simulated Pis sending STATUS, 2 CMS replicas, verify no
  dropped messages, stable DB write rate, no advisory-lock contention.

### Stage 5 — Generalise to device_command_outbox (optional, later)

Once logs have proven the outbox pattern, migrate the other
CMS→device command call sites (`sync`, `reboot`, `upgrade`,
`fetch_asset`, `clear_asset`, etc.) into a generic
`device_command_outbox`. Every command becomes durable,
retry-safe, idempotent, and auditable.

This is genuinely independent of the multi-replica work — it's a
reliability upgrade that also happens to fit this architecture.

## Rollout ordering and safety

No production customers yet, so the phasing is for engineering
reasons (each stage is independently reviewable and the system stays
working between them), not for gradual rollout. Simple forward-only
merges are fine — no feature flags, no canary, no percentage rollout.

- Stage 0 lands first and independently — immediate risk closed.
- Stages 1–3 land as regular PRs. Prod stays at 1 replica during all
  of these because that's what bicep says.
- Stage 4 flips `maxReplicas` only after the smoke tests pass in a
  staging environment scaled to 2 CMS replicas.
- **Prod cut-over to WPS is a flag day**: push Pi firmware that
  speaks WPS; same day flip the CMS deployment to `DEVICE_TRANSPORT=wps`.
  Any Pi that misses the update goes dark and gets manual intervention.
- Rollback is "revert the PR" — same as every other change in the
  repo.

## Observability additions (per stage)

All metrics exported via the OpenTelemetry SDK to App Insights.

- Stage 2: webhook receipt rate, STATUS UPDATE rate, connect-token
  mint rate, webhook HMAC failures (alert if > 0), broker
  request/response latency, Pydantic rejection counter.
- Stage 3: `log_requests` counts by status, drainer lag, retry
  histogram, blob orphan count, per-upload size histogram.
- Stage 4: leader-election churn (advisory-lock acquire/release
  rate), scheduler tick rate per replica (should be 0 on
  non-leaders), outbox drainer throughput per replica, active
  device connections per replica.

## Risks / known sharp edges

- **Payload size ceilings.** WPS webhooks capped at ~1 MB. Blob
  workflow avoids this for logs; small status acks must stay small.
  Audit in Stage 2 via the size-cap enforcement work.
- **Advisory lock release on crash.** Postgres releases at session
  end; a stuck/zombie session could delay fail-over. Acquire locks
  from a dedicated short-lived connection with `tcp_keepalives_idle`
  tuned low so dead holders are evicted quickly.
- **Webhook retry storms.** If CMS is slow (e.g., DB saturated),
  WPS retries can compound load. Rate-limit inbound webhook
  processing per device_id; shed early with 503 if we're already
  behind on that device.
- **Local broker as a new thing to maintain.** True, but the
  contract is small (~6 endpoints + webhook format) and it stops
  being a moving target once Web PubSub's contract stops changing.
  Cost: accepted.
- **Device auth gap acknowledged.** Current `device_id`-only
  handshake allows impersonation of known devices. Blast radius is
  limited (schedules + asset downloads) and mitigated by WPS JWT
  scoping + webhook HMAC. Stronger enrolment gated on SD-card
  encryption — tracked in `SECURITY.md`.

## Decision log

Resolved during the 2026-04 architecture review:

- **Option A (single-replica gateway) rejected.** Solves the
  multi-replica problem but leaves CMS with WS-termination and
  in-memory state. Requires a new container app anyway. Option D
  (managed WPS) is strictly cleaner for the same deployment cost.
- **Option C (self-hosted Redis pub/sub) rejected.** Still needs
  a WS terminator in front of Redis — same amount of code as the
  broker we're building. And it leaks Redis into CMS.
- **Centrifugo rejected.** Close to WPS semantics but not identical;
  would force two transport adapters in CMS, defeating the parity
  goal.
- **Real Azure WPS F1 SKU in compose rejected.** Can't kill-test,
  caps at 20 connections globally, requires outbound net + creds
  in CI. We build our own broker for compose/CI; F1 is still the
  starting SKU for **prod** (cut-over to S1 when fleet grows).
- **Redis inside the broker rejected.** Would only exist to let the
  broker itself scale past one replica in smoke tests. The multi-
  replica bugs live in the CMS, not the broker; testing N CMS
  replicas against a single broker replica is sufficient. Saves
  ~200 LoC and a Redis dep in compose.
- **pg NOTIFY cross-replica UI fan-out rejected.** UI polls
  `/api/devices` every 5 s (verified in `cms/templates/devices.html`);
  no SSE or browser WS anywhere. Every replica serves the poll
  directly from Postgres. Saves ~300 LoC in Stage 4.
- **SAS-direct blob upload rejected in favour of CMS-proxy upload.**
  Measured log payload is ~92 KB gzipped for 7 days on a test Pi.
  Even 1000 devices uploading simultaneously is ~100 MB of CMS
  memory, never realistic. CMS-proxy works identically with Azurite
  and Azure Blob, no user-delegation key plumbing.
- **`processed_events` dedup table rejected.** All current inbound
  message types are idempotent by construction. Monotonic guard on
  STATUS is sufficient. Revisit only if a non-idempotent handler is
  introduced.
- **Uniform advisory-lock gating of all loops rejected.** Per-loop
  analysis (leader / SKIP LOCKED / replicated) done in Stage 1.
  Uniform locking would serialize outbox drainers, defeating the
  point of multi-replica.
- **Strong device authentication (per-device keypair) deferred.**
  SD cards are unencrypted so on-device credentials are already
  physically extractable; blast radius of impersonation today is
  low. Investment better spent on WSS input hardening + webhook
  HMAC. Tracked in `SECURITY.md`.
- **Separate Azure storage account for logs rejected.** Storage
  accounts are free on Azure (cost is per-GB/transaction, identical
  either way). At our scale the isolation benefit doesn't justify
  the extra Bicep resource. New container in the existing account.
- **Dual-endpoint rollout (legacy `/ws/device` + WPS webhook)
  rejected.** Small internal fleet, we control firmware updates.
  Flag-day cut-over. Saves ~300 LoC of legacy-path maintenance.
- **App Insights direct SDK rejected in favour of OpenTelemetry →
  App Insights exporter.** Same backend, but keeps the door open
  for alternate observability vendors with a config change. Small
  extra setup cost.
- **WPS S1 from day one rejected in favour of starting on F1.**
  F1 (free, 20 concurrent connections) covers the current internal
  test fleet. Upgrade to S1 ($48/mo) is a single Bicep parameter
  flip when we outgrow F1. Document the threshold in the runbook.
- **`VACUUM FULL` on the devices table rejected.** `ALTER TABLE ...
  SET (fillfactor=80)` applies to new pages only. Existing
  devices table is too small (<1 MB) for the difference to matter;
  rows get rewritten organically as STATUS updates flow through.
  No maintenance window needed.
- **Per-feature outbox tables vs generic device_command_outbox.**
  Chose per-feature (`log_requests`) for Stage 3 pragmatism;
  generic table as Stage 5 consolidation once the shape is proven.
