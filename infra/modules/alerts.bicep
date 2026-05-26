// ──────────────────────────────────────────────────────────────
// alerts.bicep — Phase 0 / A1.5 of the telemetry roadmap (#474)
//
// Provisions:
//   • Email action group (single recipient — fleet on-call)
//   • Four scheduled query (log-search) alert rules backed by the
//     workspace-based Application Insights tables:
//       1. CMS 5xx response spike
//       2. Slow request latency (p95 > 3s sustained)
//       3. Dependency call failures (DB / downstream)
//       4. Unhandled exceptions
//   • One workbook with the same four panels for at-a-glance triage
//
// All KQL queries deliberately exclude /health and /metrics so probe
// traffic doesn't drown signal. KQL `!contains` is used (not `!has`,
// which is term-based and won't match e.g. `/healthz` against
// `/health`).
//
// Thresholds are intentionally conservative for a no-real-traffic
// deployment — we'd rather get paged once and tune up than miss
// something. The heartbeat alert specifically guards against the
// "nothing is emitting telemetry" outage that the four signal-based
// rules would silently miss.
// ──────────────────────────────────────────────────────────────

@description('Azure region for the alert resources (workbook in particular).')
param location string

@description('Resource ID of the workspace-based Application Insights component the alerts query against.')
param appInsightsId string

@description('Friendly name of the Application Insights component (used in workbook titles).')
param appInsightsName string

@description('Email address that receives all alert notifications. Empty disables the action group + alert rules entirely (useful for dev environments).')
param alertEmail string = ''

@description('Common tags applied to every alert resource.')
param tags object = {}

@description('Short identifier prefix used to name alert resources (e.g. "agoracms").')
param namePrefix string

@description('Public FQDN of the CMS container app (e.g. agoragwdev-cms.<env>.azurecontainerapps.io or a custom domain). Required when useSyntheticHeartbeat=true; ignored otherwise.')
param cmsFqdn string = ''

@description('When true, deploy an Application Insights standard availability test against https://<cmsFqdn>/healthz and switch the heartbeat alert to fire on probe failures. When false (legacy), the heartbeat alert fires on absence of any AppRequests — which produces false positives on idle environments because the OTel SDK filters probe traffic before ingestion.')
param useSyntheticHeartbeat bool = false

var alertsEnabled = !empty(alertEmail)
var syntheticHeartbeatEnabled = alertsEnabled && useSyntheticHeartbeat && !empty(cmsFqdn)

// ──────────────────────────────────────────────────────────────
// Action Group — email-only for now. Webhooks/SMS can be layered
// in later without breaking existing alert rule references.
// ──────────────────────────────────────────────────────────────
resource actionGroup 'Microsoft.Insights/actionGroups@2023-01-01' = if (alertsEnabled) {
  name: '${namePrefix}-ag-fleet'
  location: 'global'
  tags: tags
  properties: {
    groupShortName: 'agorafleet'
    enabled: true
    emailReceivers: [
      {
        name: 'fleet-oncall'
        emailAddress: alertEmail
        useCommonAlertSchema: true
      }
    ]
  }
}

// ──────────────────────────────────────────────────────────────
// Helper: common action group block for every alert rule.
// ──────────────────────────────────────────────────────────────
var actionsBlock = alertsEnabled ? {
  actionGroups: [
    actionGroup.id
  ]
} : {
  actionGroups: []
}

// ──────────────────────────────────────────────────────────────
// 1. CMS 5xx spike — > 5 server errors in any 5-minute window.
//    Excludes health/metrics probes so noise from container probes
//    doesn't trigger pages.
// ──────────────────────────────────────────────────────────────
resource alert5xx 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (alertsEnabled) {
  name: '${namePrefix}-alert-cms-5xx'
  location: location
  tags: tags
  properties: {
    displayName: 'CMS 5xx response spike'
    description: 'Triggers when the CMS returns more than 5 HTTP 5xx responses in a 5-minute window. Excludes /health and /metrics probes.'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    scopes: [
      appInsightsId
    ]
    criteria: {
      allOf: [
        {
          query: 'requests\n| where name !contains "/health" and name !contains "/metrics"\n| where toint(resultCode) >= 500\n| summarize Errors = count() by bin(timestamp, 5m)'
          timeAggregation: 'Total'
          metricMeasureColumn: 'Errors'
          operator: 'GreaterThan'
          threshold: 5
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: actionsBlock
  }
}

// ──────────────────────────────────────────────────────────────
// 2. Slow requests — p95 latency > 3 s over the last 15 min,
//    minimum 20 samples to avoid firing on a single slow call.
// ──────────────────────────────────────────────────────────────
resource alertLatency 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (alertsEnabled) {
  name: '${namePrefix}-alert-cms-latency'
  location: location
  tags: tags
  properties: {
    displayName: 'CMS slow requests (p95 > 3s)'
    description: 'Triggers when 95th-percentile request latency exceeds 3 seconds over a 15-minute window with at least 20 samples. Excludes /health and /metrics probes.'
    severity: 3
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    scopes: [
      appInsightsId
    ]
    criteria: {
      allOf: [
        {
          query: 'requests\n| where name !contains "/health" and name !contains "/metrics"\n| summarize Samples = count(), P95Ms = percentile(duration, 95) by bin(timestamp, 5m)\n| where Samples >= 20\n| project timestamp, P95Ms'
          timeAggregation: 'Maximum'
          metricMeasureColumn: 'P95Ms'
          operator: 'GreaterThan'
          threshold: 3000
          failingPeriods: {
            numberOfEvaluationPeriods: 3
            minFailingPeriodsToAlert: 2
          }
        }
      ]
    }
    autoMitigate: true
    actions: actionsBlock
  }
}

// ──────────────────────────────────────────────────────────────
// 3. Dependency failures — DB calls, outbound HTTP, etc.
// ──────────────────────────────────────────────────────────────
resource alertDeps 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (alertsEnabled) {
  name: '${namePrefix}-alert-cms-deps'
  location: location
  tags: tags
  properties: {
    displayName: 'CMS dependency failures'
    description: 'Triggers when more than 5 outbound dependency calls (DB, HTTP, etc.) fail in a 5-minute window.'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    scopes: [
      appInsightsId
    ]
    criteria: {
      allOf: [
        {
          query: 'dependencies\n| where operation_Name !contains "/health" and operation_Name !contains "/metrics"\n| where success == false\n| summarize Failures = count() by bin(timestamp, 5m)'
          timeAggregation: 'Total'
          metricMeasureColumn: 'Failures'
          operator: 'GreaterThan'
          threshold: 5
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: actionsBlock
  }
}

// ──────────────────────────────────────────────────────────────
// 4. Unhandled exceptions — any spike in AppExceptions.
// ──────────────────────────────────────────────────────────────
resource alertExceptions 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (alertsEnabled) {
  name: '${namePrefix}-alert-cms-exceptions'
  location: location
  tags: tags
  properties: {
    displayName: 'CMS unhandled exceptions'
    description: 'Triggers when more than 3 unhandled exceptions are logged by the CMS in a 5-minute window.'
    severity: 2
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT5M'
    scopes: [
      appInsightsId
    ]
    criteria: {
      allOf: [
        {
          query: 'exceptions\n| where operation_Name !contains "/health" and operation_Name !contains "/metrics"\n| summarize Exceptions = count() by bin(timestamp, 5m)'
          timeAggregation: 'Total'
          metricMeasureColumn: 'Exceptions'
          operator: 'GreaterThan'
          threshold: 3
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: actionsBlock
  }
}

// ──────────────────────────────────────────────────────────────
// 5. Heartbeat — telemetry silence. If we see ZERO AppRequests
//    of ANY kind across a 15-minute window, something is very
//    wrong (CMS down, ingress broken, App Insights ingestion
//    failing, OTEL exporter stuck). The other four rules can't
//    catch this because they all depend on telemetry actually
//    arriving. We deliberately do NOT exclude /health probes
//    here — Container Apps probes run constantly, so their
//    absence is itself the strongest "CMS is unreachable or
//    not exporting" signal we have, especially while the
//    deployment has no real customer traffic.
//
//    `| count` always emits a single row with Count = 0 even
//    when the table is empty, which is what makes the LessThan
//    comparison fire instead of silently returning no rows.
// ──────────────────────────────────────────────────────────────
// ──────────────────────────────────────────────────────────────
// Synthetic availability test (preferred heartbeat signal).
//
// AppInsights standard test pings /healthz from multiple Azure
// regions every 5 minutes.  This is independent of in-app
// telemetry, so it works correctly on idle environments where the
// OTel SDK filters probe traffic and there is no user/device
// activity.  The companion heartbeat alert below switches its
// query to availabilityResults when this is enabled.
// ──────────────────────────────────────────────────────────────
resource webtestCmsHealthz 'Microsoft.Insights/webtests@2022-06-15' = if (syntheticHeartbeatEnabled) {
  name: '${namePrefix}-webtest-cms-healthz'
  location: location
  tags: union(tags, {
    'hidden-link:${appInsightsId}': 'Resource'
  })
  kind: 'standard'
  properties: {
    SyntheticMonitorId: '${namePrefix}-webtest-cms-healthz'
    Name: 'CMS /healthz availability'
    Description: 'External synthetic probe of /healthz from multiple Azure regions. Drives the CMS heartbeat alert.'
    Enabled: true
    Frequency: 300
    Timeout: 30
    Kind: 'standard'
    RetryEnabled: true
    Locations: [
      { Id: 'us-ca-sjc-azr' }   // West US
      { Id: 'us-tx-sn1-azr' }   // South Central US
      { Id: 'us-va-ash-azr' }   // East US
    ]
    Request: {
      RequestUrl: 'https://${cmsFqdn}/healthz'
      HttpVerb: 'GET'
      ParseDependentRequests: false
    }
    ValidationRules: {
      ExpectedHttpStatusCode: 200
      SSLCheck: true
      SSLCertRemainingLifetimeCheck: 7
      ContentValidation: {
        ContentMatch: '"status":"ok"'
        IgnoreCase: false
        PassIfTextFound: true
      }
    }
  }
}

resource alertHeartbeat 'Microsoft.Insights/scheduledQueryRules@2023-03-15-preview' = if (alertsEnabled) {
  name: '${namePrefix}-alert-cms-heartbeat'
  location: location
  tags: tags
  properties: {
    displayName: syntheticHeartbeatEnabled ? 'CMS heartbeat (synthetic probe failing)' : 'CMS heartbeat (no telemetry)'
    description: syntheticHeartbeatEnabled
      ? 'Fires when the AppInsights standard availability test against /healthz fails from 2+ source regions over a 15-minute window. Replaces the prior request-count heartbeat, which produced false positives on idle environments because the OTel SDK filters probe traffic before ingestion.'
      : 'Triggers when the CMS emits zero AppRequests of any kind (including probes) over a 15-minute window. Probes are intentionally NOT excluded here — their absence is the canonical "the service is gone" signal.'
    severity: 1
    enabled: true
    evaluationFrequency: 'PT5M'
    windowSize: 'PT15M'
    scopes: [
      appInsightsId
    ]
    criteria: {
      allOf: syntheticHeartbeatEnabled ? [
        {
          query: 'availabilityResults\n| where success == false\n| summarize FailingLocations = dcount(location)'
          timeAggregation: 'Total'
          metricMeasureColumn: 'FailingLocations'
          operator: 'GreaterThanOrEqual'
          threshold: 2
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ] : [
        {
          query: 'requests\n| count'
          timeAggregation: 'Total'
          metricMeasureColumn: 'Count'
          operator: 'LessThan'
          threshold: 1
          failingPeriods: {
            numberOfEvaluationPeriods: 1
            minFailingPeriodsToAlert: 1
          }
        }
      ]
    }
    autoMitigate: true
    actions: actionsBlock
  }
}

// ──────────────────────────────────────────────────────────────
// Workbook— single triage page mirroring the four alerts plus
// a request-volume overview. Bicep stores the layout as a
// serialised JSON blob; this is the minimum viable shape.
// ──────────────────────────────────────────────────────────────
var workbookContent = {
  version: 'Notebook/1.0'
  items: [
    {
      type: 1
      content: {
        json: '## Agora CMS — Telemetry triage\nLive view of the four alert signals plus a request-volume overview. Backed by Application Insights component `${appInsightsName}`.'
      }
      name: 'header'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'requests\n| where name !contains "/health" and name !contains "/metrics"\n| summarize Total = count(), Errors = countif(toint(resultCode) >= 500) by bin(timestamp, 5m)\n| order by timestamp asc'
        size: 0
        title: 'Request volume & 5xx errors (5-min bins)'
        timeContext: {
          durationMs: 86400000
        }
        queryType: 0
        resourceType: 'microsoft.insights/components'
        visualization: 'timechart'
      }
      name: 'requests-volume'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'requests\n| where name !contains "/health" and name !contains "/metrics"\n| summarize P50 = percentile(duration, 50), P95 = percentile(duration, 95), P99 = percentile(duration, 99) by bin(timestamp, 5m)\n| order by timestamp asc'
        size: 0
        title: 'Request latency p50/p95/p99 (ms)'
        timeContext: {
          durationMs: 86400000
        }
        queryType: 0
        resourceType: 'microsoft.insights/components'
        visualization: 'timechart'
      }
      name: 'latency'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'dependencies\n| where operation_Name !contains "/health" and operation_Name !contains "/metrics"\n| summarize Total = count(), Failures = countif(success == false) by bin(timestamp, 5m), type\n| order by timestamp asc'
        size: 0
        title: 'Dependency calls & failures by type'
        timeContext: {
          durationMs: 86400000
        }
        queryType: 0
        resourceType: 'microsoft.insights/components'
        visualization: 'timechart'
      }
      name: 'deps'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'exceptions\n| where operation_Name !contains "/health" and operation_Name !contains "/metrics"\n| summarize Count = count() by bin(timestamp, 5m), problemId\n| order by timestamp asc'
        size: 0
        title: 'Exceptions by problem id'
        timeContext: {
          durationMs: 86400000
        }
        queryType: 0
        resourceType: 'microsoft.insights/components'
        visualization: 'timechart'
      }
      name: 'exceptions'
    }
    // ── CMS internals (Pillar B counters) ─────────────────────────
    // Surfaces the OpenTelemetry counters emitted from
    // ``cms/metrics.py``: WPS send health (B1), scheduler tick health
    // (B2), and leader-lease state changes (B-presence).  All
    // counters land in the App Insights resource-scoped
    // ``customMetrics`` table; ``sum(value)`` aggregates the
    // exported delta points back into per-bin totals.
    {
      type: 1
      content: {
        json: '## CMS internals — Pillar B counters\nWPS send health, scheduler tick health, and leader-lease state changes emitted from ``cms.metrics``. Empty series labelled ``unknown`` indicate an instrumentation regression (counter incremented without the expected attribute).'
      }
      name: 'b-counters-header'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'customMetrics\n| where name in ("agora.wps.send.attempt","agora.wps.send.success","agora.wps.send.failure")\n| extend outcome = case(\n    name == "agora.wps.send.attempt", "attempt",\n    name == "agora.wps.send.success", "success",\n    name == "agora.wps.send.failure", "failure",\n    name)\n| summarize Count = sum(value) by bin(timestamp, 5m), outcome\n| order by timestamp asc'
        size: 0
        title: 'WPS send rate by outcome (attempt / success / failure)'
        timeContext: {
          durationMs: 86400000
        }
        queryType: 0
        resourceType: 'microsoft.insights/components'
        visualization: 'timechart'
      }
      name: 'wps-rate'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'customMetrics\n| where name in ("agora.wps.send.success","agora.wps.send.failure")\n| summarize\n    Success = sumif(value, name == "agora.wps.send.success"),\n    Failure = sumif(value, name == "agora.wps.send.failure")\n  by bin(timestamp, 5m)\n| extend Total = Success + Failure\n| extend FailurePct = iff(Total > 0, 100.0 * Failure / Total, real(null))\n| project timestamp, FailurePct\n| order by timestamp asc'
        size: 0
        title: 'WPS failure % (failure / (success + failure))'
        timeContext: {
          durationMs: 86400000
        }
        queryType: 0
        resourceType: 'microsoft.insights/components'
        visualization: 'timechart'
      }
      name: 'wps-failure-pct'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'customMetrics\n| where name == "agora.wps.send.failure"\n| extend reason = tostring(customDimensions.reason)\n| extend reason = iff(isempty(reason), "unknown", reason)\n| summarize Count = sum(value) by bin(timestamp, 5m), reason\n| order by timestamp asc'
        size: 0
        title: 'WPS failures by reason (404 / 429 / http_error / unexpected)'
        timeContext: {
          durationMs: 86400000
        }
        queryType: 0
        resourceType: 'microsoft.insights/components'
        visualization: 'timechart'
      }
      name: 'wps-failures-by-reason'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'customMetrics\n| where name == "agora.scheduler.tick"\n| extend outcome = tostring(customDimensions.outcome)\n| extend outcome = iff(isempty(outcome), "unknown", outcome)\n| summarize Count = sum(value) by bin(timestamp, 5m), outcome\n| order by timestamp asc'
        size: 0
        title: 'Scheduler tick by outcome (evaluated / skipped_not_leader / error)'
        timeContext: {
          durationMs: 86400000
        }
        queryType: 0
        resourceType: 'microsoft.insights/components'
        visualization: 'timechart'
      }
      name: 'scheduler-tick'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'customMetrics\n| where name == "agora.scheduler.missed_emitted"\n| summarize Count = sum(value) by bin(timestamp, 5m)\n| order by timestamp asc'
        size: 0
        title: 'Scheduler MISSED schedules emitted'
        timeContext: {
          durationMs: 86400000
        }
        queryType: 0
        resourceType: 'microsoft.insights/components'
        visualization: 'timechart'
      }
      name: 'scheduler-missed'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'customMetrics\n| where name in ("agora.presence.claim","agora.presence.claim_lost")\n| extend loop = tostring(customDimensions.loop_name)\n| extend loop = iff(isempty(loop), "unknown", loop)\n| extend event = case(\n    name == "agora.presence.claim", "claim",\n    name == "agora.presence.claim_lost", "claim_lost",\n    name)\n| summarize Count = sum(value) by bin(timestamp, 5m), event, loop\n| order by timestamp asc'
        size: 0
        title: 'Leader lease state changes (claim / claim_lost) by loop'
        timeContext: {
          durationMs: 86400000
        }
        queryType: 0
        resourceType: 'microsoft.insights/components'
        visualization: 'timechart'
      }
      name: 'presence-lease'
    }
    {
      type: 3
      content: {
        version: 'KqlItem/1.0'
        query: 'customMetrics\n| where name == "agora.presence.heartbeat_late"\n| extend loop = tostring(customDimensions.loop_name)\n| extend loop = iff(isempty(loop), "unknown", loop)\n| summarize Count = sum(value) by bin(timestamp, 5m), loop\n| order by timestamp asc'
        size: 0
        title: 'Leader lease heartbeat_late by loop (renewal exceptions)'
        timeContext: {
          durationMs: 86400000
        }
        queryType: 0
        resourceType: 'microsoft.insights/components'
        visualization: 'timechart'
      }
      name: 'presence-heartbeat-late'
    }
  ]
  styleSettings: {}
  '$schema': 'https://github.com/Microsoft/Application-Insights-Workbooks/blob/master/schema/workbook.json'
}

resource workbook 'Microsoft.Insights/workbooks@2023-06-01' = {
  name: guid(resourceGroup().id, 'cms-telemetry-triage')
  location: location
  tags: tags
  kind: 'shared'
  properties: {
    displayName: 'Agora CMS — Telemetry triage'
    serializedData: string(workbookContent)
    version: '1.1'
    sourceId: appInsightsId
    category: 'workbook'
  }
}

// ──────────────────────────────────────────────────────────────
// Outputs
// ──────────────────────────────────────────────────────────────
output actionGroupId string = alertsEnabled ? actionGroup.id : ''
output workbookId string = workbook.id
