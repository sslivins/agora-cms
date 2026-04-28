# Observability — CMS telemetry pipeline

Issue [#474](../../../issues/474) — Phase 0 (A1 + A1.5).

## What's wired up

The CMS app is auto-instrumented at process start by the
[`azure-monitor-opentelemetry`](https://pypi.org/project/azure-monitor-opentelemetry/)
distro.  See `cms/observability.py` for the bootstrap; it runs from
`cms/main.py` before the FastAPI app is constructed.

Auto-instrumentations active:

| Library     | What it captures                                                        | App Insights table |
|-------------|-------------------------------------------------------------------------|--------------------|
| FastAPI     | Every HTTP request: method, route template, status, duration, client IP | `requests`         |
| SQLAlchemy  | Every database query: statement preview, duration, server, success      | `dependencies`     |
| HTTPX       | Outbound HTTP calls (e.g. CMS → Azure Web PubSub REST)                  | `dependencies`     |
| Logging     | Stdlib `logging` records (WARNING+) emitted anywhere in the process      | `traces`           |
| Exceptions  | Unhandled exceptions inside instrumented frames                          | `exceptions`       |

Sampling is **100%** for the A1 baseline.  Sampling will be revisited in
a later phase once we know what the actual ingestion cost looks like.

## How to enable / disable

Driven by env vars on the Container Apps deployment:

* `APPLICATIONINSIGHTS_CONNECTION_STRING` — provided automatically by the
  Bicep template (the `appInsights` resource in
  `infra/modules/containerApps.bicep`).  When unset, telemetry export is
  silently disabled — local dev and docker-compose runs work normally.
* `OTEL_RESOURCE_ATTRIBUTES` — set in Bicep to
  `service.name=agora-cms,service.namespace=agora,deployment.environment=<env>`
  so all rows are tagged with the deployment's environment name.
* `AGORA_CMS_DISABLE_OBSERVABILITY=1` — escape hatch to disable the SDK
  even when the conn string is set.  Useful for one-off debugging.

## Verifying after a deploy

1. Open the resource group in the portal and find the App Insights
   resource (`<env>-ai`).
2. Go to **Logs** and run:

   ```kql
   requests
   | where timestamp > ago(15m)
   | summarize count() by name, resultCode
   | order by count_ desc
   ```

   You should see `GET /healthz` rows within a few minutes — Container
   Apps liveness probes hit the endpoint constantly.

3. Confirm dependency tracking:

   ```kql
   dependencies
   | where timestamp > ago(15m)
   | summarize count() by type, name
   ```

   Expect both `Microsoft Postgres SQL Server` (or `postgres`) rows from
   SQLAlchemy and any HTTP rows from the WPS REST client.

4. Confirm exceptions are flowing if you can manufacture one (e.g. hit
   an endpoint that raises):

   ```kql
   exceptions
   | where timestamp > ago(15m)
   | project timestamp, type, outerMessage, operation_Name
   ```

## Troubleshooting

* **No rows showing up after 5 minutes** — check the CMS container
  startup logs for the line `App Insights observability enabled`.  If
  it instead says `… not set; App Insights export disabled`, the
  connection string env var didn't reach the container.  Confirm the
  Bicep deployment ran cleanly and the `appInsights` resource exists.
* **Logs show `azure-monitor-opentelemetry not installed`** — the
  Docker image was built against an older `requirements.txt`.  Rebuild
  and redeploy.
* **Bursts of 4xx / 5xx in `requests`** — A1.5 alert rules now page the
  on-call email when these spike.  See _Alerts and workbook_ below.

## Alerts and workbook (A1.5)

Provisioned by `infra/modules/alerts.bicep` and wired in from
`infra/main.bicep`.  Recipient is supplied by the CD pipeline from the
GitHub repo variable `ALERT_EMAIL` and forwarded to the bicep
`alertEmail` parameter; the address itself is **not** committed to
this repo.  Setting `ALERT_EMAIL` to an empty string disables the
action group and all alert rules, useful for short-lived dev
environments.  The workbook is always provisioned regardless of
`alertEmail` so dashboards remain available even when paging is off.

The five alert rules all query the workspace-based App Insights tables
(`AppRequests`, `AppDependencies`, `AppExceptions`).  Signal-based
rules use `!contains "/health"` and `!contains "/metrics"` so probe
traffic never trips them.  The heartbeat rule is the deliberate
exception — it counts probes too, because their absence is itself the
strongest "service is gone" signal.

| Rule                            | Severity | Window | Threshold                                      | Why this threshold |
|---------------------------------|----------|--------|------------------------------------------------|--------------------|
| CMS heartbeat (no telemetry)    | 1        | 15 min | `AppRequests` count `< 1` (probes counted)     | Catches the worst-case outage where nothing is emitting telemetry — the four signal-based rules would silently miss it |
| CMS 5xx response spike          | 2        | 5 min  | `> 5` 5xx responses in any 5-min bin           | A handful is noise; 5+ usually indicates a real bug or downstream failure |
| CMS slow requests (p95 > 3s)    | 3        | 15 min | p95 > 3000 ms across ≥ 20 non-probe samples in the rolling window, 2 of 3 evaluations | Bursty single slow calls don't page; sustained latency does |
| CMS dependency failures         | 2        | 5 min  | `> 5` failed deps (DB / outbound HTTP) in 5 min | DB or WPS REST falling over should page immediately |
| CMS unhandled exceptions        | 2        | 5 min  | `> 3` exceptions in 5 min                      | Rare exceptions are tolerable; a sustained source is not |

A companion workbook ("Agora CMS — Telemetry triage") is provisioned
alongside the alerts and shows the same signals plus a 24-hour
request-volume timechart.  Find it in the App Insights resource under
**Workbooks → Shared**.

## What's next

* **Phase 1** — Pi-side telemetry (devices report their own request /
  exception / playback metrics).  Will arrive in `requests` /
  `customEvents` from the device fleet, queryable by `client_Id`.
