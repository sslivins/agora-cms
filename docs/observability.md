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
* **Bursts of 4xx / 5xx in `requests`** — A1.5 alert rules are designed
  to catch these.  Until A1.5 lands, eyeball the workbook manually.

## What's next

* **A1.5** — health workbook and alert rules (issue #474, same phase).
* **Phase 1** — Pi-side telemetry (devices report their own request /
  exception / playback metrics).  Will arrive in `requests` /
  `customEvents` from the device fleet, queryable by `client_Id`.
