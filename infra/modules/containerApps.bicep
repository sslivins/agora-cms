// ──────────────────────────────────────────────────────────────
// containerApps.bicep — Container Apps Environment + CMS + MCP apps
// ──────────────────────────────────────────────────────────────

param location string
param environmentName string
param containerAppsSubnetId string
@description('CIDR of the Container Apps infrastructure subnet. Passed to the CMS container as FORWARDED_ALLOW_IPS so uvicorn only trusts X-Forwarded-For from the envoy ingress hops inside this subnet.')
param containerAppsSubnetCidr string
param tags object = {}

// ── CMS App config ──
param cmsAppName string
param cmsImage string
param cmsCpu string = '1.0'
param cmsMemory string = '2Gi'
@secure()
param cmsSecretKey string
@secure()
param cmsDatabaseUrl string
@secure()
param cmsAdminUsername string
@secure()
param cmsAdminPassword string
param cmsApiKeyRotationHours string = '24'
@secure()
param storageConnectionString string
param storageBlobEndpoint string

// ── Azure Files mount ──
param storageAccountName string
param transcodeShareName string
@secure()
param storageAccountKey string

// ── MCP App config ──
param mcpAppName string
param mcpImage string

// ── Worker Job config ──
param workerJobName string
param workerImage string
param workerCpu string = '4.0'
param workerMemory string = '8Gi'

// ── Azure Key Vault (service key exchange) ──
param keyVaultUri string = ''

// ── Azure Web PubSub (device transport) ──
@description('Azure Web PubSub connection string used by CMS to relay device traffic through WPS. When empty, CMS falls back to direct /ws/device websocket mode.')
@secure()
param wpsConnectionString string = ''

@description('Device transport mode: "wps" routes all device traffic through Azure Web PubSub (multi-replica safe); "local" uses direct CMS→device websockets (single-replica only).')
@allowed(['wps', 'local'])
param deviceTransport string = 'wps'

@description('GitHub personal access token used by the CMS "Report an issue" feature. When empty, the report-issue button is hidden.')
@secure()
param githubIssuesToken string = ''

@description('Public CMS URL override (e.g. https://agora.example.com, no trailing slash). When non-empty, takes precedence over the auto-derived "https://<cmsAppName>.<defaultDomain>" for AGORA_CMS_BASE_URL -- needed when a custom domain fronts the app so invite/setup-account links, imager URLs, and the wss://host/ws/device baked into Pi fleet env all point at the real public hostname.')
param cmsBaseUrlOverride string = ''

// ── Azure OpenAI (Assistant feature) ──
@description('Azure OpenAI endpoint URL (e.g. https://agoragwdev-aoai.openai.azure.com/). Empty when the Assistant feature is not deployed in this environment; CMS treats empty as "feature disabled" at runtime.')
param azureOpenAIEndpoint string = ''

@description('Azure OpenAI deployment name to use for chat completions (matches the deployment provisioned in azureOpenAI.bicep, default "chat"). Empty when the Assistant feature is not deployed in this environment.')
param azureOpenAIDeployment string = ''

// ── Blue/green deploy controls (Multiple revision mode) ──
@description('Revision suffix for the CMS Container App. Each deploy MUST pass a unique value (e.g. "v1-12-34") so a brand-new revision is created at 0% traffic. The workflow flips traffic to it after smoke probes pass.')
param cmsRevisionSuffix string = ''

@description('Name of the existing CMS revision that should keep 100% traffic during this deploy. The new revision lands at 0% traffic; the workflow flips traffic post-verify. Empty for bootstrap/first-deploy.')
param previousCmsRevisionName string = ''

@description('Revision suffix for the MCP Container App. See cmsRevisionSuffix for semantics.')
param mcpRevisionSuffix string = ''

@description('Name of the existing MCP revision that should keep 100% traffic during this deploy. See previousCmsRevisionName for semantics.')
param previousMcpRevisionName string = ''

// ── Container Apps Environment ──
resource logAnalytics 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: '${environmentName}-logs'
  location: location
  tags: tags
  properties: {
    sku: {
      name: 'PerGB2018'
    }
    retentionInDays: 30
  }
}

// ── Application Insights (workspace-based) ──
// Phase 0 / A1 of the telemetry roadmap (issue #474).  Auto-instruments
// the CMS app via OpenTelemetry — request/dependency/exception tables
// are populated without any per-route code changes.  Reuses the
// Container Apps log analytics workspace so all telemetry lives in one
// place and is queryable with a single KQL connection.
resource appInsights 'Microsoft.Insights/components@2020-02-02' = {
  name: '${environmentName}-ai'
  location: location
  tags: tags
  kind: 'web'
  properties: {
    Application_Type: 'web'
    WorkspaceResourceId: logAnalytics.id
    IngestionMode: 'LogAnalytics'
    publicNetworkAccessForIngestion: 'Enabled'
    publicNetworkAccessForQuery: 'Enabled'
  }
}

resource containerAppsEnv 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: environmentName
  location: location
  tags: tags
  properties: {
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logAnalytics.properties.customerId
        sharedKey: logAnalytics.listKeys().primarySharedKey
      }
    }
    vnetConfiguration: {
      infrastructureSubnetId: containerAppsSubnetId
      internal: false
    }
  }
}

// Mount Azure Files share for FFmpeg transcode workspace
resource transcodeStorage 'Microsoft.App/managedEnvironments/storages@2024-03-01' = {
  parent: containerAppsEnv
  name: 'transcode-storage'
  properties: {
    azureFile: {
      accountName: storageAccountName
      accountKey: storageAccountKey
      shareName: transcodeShareName
      accessMode: 'ReadWrite'
    }
  }
}

// ── CMS Container App ──
resource cmsApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: cmsAppName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      // Blue/green: every deploy creates a new revision at 0% traffic; the
      // workflow flips traffic to it only after smoke probes pass against
      // its per-revision FQDN. A failed deploy never reaches users because
      // the previous revision keeps serving until traffic is flipped.
      activeRevisionsMode: 'Multiple'
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto' // supports both HTTP and WebSocket
        allowInsecure: false
        // When previousCmsRevisionName is supplied (the common case after the
        // first blue/green deploy), pin 100% traffic to that named revision so
        // the new one lands silent. Bootstrap (no previous revision known)
        // falls back to "100% to latest" so the very first multi-mode deploy
        // still serves traffic.
        traffic: !empty(previousCmsRevisionName) ? [
          {
            revisionName: previousCmsRevisionName
            weight: 100
          }
        ] : [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      secrets: [
        {
          name: 'cms-database-url'
          value: cmsDatabaseUrl
        }
        {
          name: 'cms-secret-key'
          value: cmsSecretKey
        }
        {
          name: 'cms-admin-username'
          value: cmsAdminUsername
        }
        {
          name: 'cms-admin-password'
          value: cmsAdminPassword
        }
        {
          name: 'storage-connection-string'
          value: storageConnectionString
        }
        {
          name: 'wps-connection-string'
          value: wpsConnectionString
        }
        {
          name: 'github-issues-token'
          value: githubIssuesToken
        }
        {
          name: 'app-insights-connection-string'
          value: appInsights.properties.ConnectionString
        }
      ]
    }
    template: {
      // Unique per-deploy suffix so a brand-new revision is created (and
      // reachable via its own per-revision FQDN) even when nothing else in
      // the template changed. Empty string is treated as "auto-generate" by
      // ACA, which is the right behaviour for local Bicep what-if runs.
      revisionSuffix: cmsRevisionSuffix
      containers: [
        {
          name: 'cms'
          image: cmsImage
          resources: {
            cpu: json(cmsCpu)
            memory: cmsMemory
          }
          env: [
            {
              name: 'AGORA_CMS_DATABASE_URL'
              secretRef: 'cms-database-url'
            }
            {
              name: 'AGORA_CMS_SECRET_KEY'
              secretRef: 'cms-secret-key'
            }
            {
              name: 'AGORA_CMS_ADMIN_USERNAME'
              secretRef: 'cms-admin-username'
            }
            {
              name: 'AGORA_CMS_ADMIN_PASSWORD'
              secretRef: 'cms-admin-password'
            }
            {
              name: 'AGORA_CMS_API_KEY_ROTATION_HOURS'
              value: cmsApiKeyRotationHours
            }
            {
              name: 'AGORA_CMS_STORAGE_BACKEND'
              value: 'azure'
            }
            {
              name: 'AGORA_CMS_AZURE_STORAGE_CONNECTION_STRING'
              secretRef: 'storage-connection-string'
            }
            {
              name: 'AGORA_CMS_AZURE_STORAGE_BLOB_ENDPOINT'
              value: storageBlobEndpoint
            }
            {
              name: 'AGORA_CMS_MCP_SERVER_URL'
              value: 'http://${mcpAppName}'
            }
            {
              name: 'AGORA_CMS_AZURE_KEYVAULT_URI'
              value: keyVaultUri
            }
            {
              // Only trust X-Forwarded-For from the envoy ingress hops running
              // inside the Container Apps infrastructure subnet. Prevents a
              // caller who bypasses the managed ingress from spoofing their
              // source IP in audit logs.
              name: 'FORWARDED_ALLOW_IPS'
              value: containerAppsSubnetCidr
            }
            {
              name: 'AGORA_CMS_DEVICE_TRANSPORT'
              value: deviceTransport
            }
            {
              name: 'AGORA_CMS_WPS_CONNECTION_STRING'
              secretRef: 'wps-connection-string'
            }
            {
              name: 'AGORA_CMS_GITHUB_ISSUES_TOKEN'
              secretRef: 'github-issues-token'
            }
            {
              // Public URL of the CMS -- the value used for invite-email
              // setup-account links, the imager router's wss://host/ws/device
              // URL baked into provisioned Pis, and any other place that
              // needs an absolute URL. When cmsBaseUrlOverride is set (e.g.
              // 'https://agora.example.com' for a custom-domain deploy) it
              // wins; otherwise we auto-derive the Azure default-domain URL
              // from the container app name + the managed environment.
              name: 'AGORA_CMS_BASE_URL'
              value: empty(cmsBaseUrlOverride) ? 'https://${cmsAppName}.${containerAppsEnv.properties.defaultDomain}' : cmsBaseUrlOverride
            }
            {
              // Picked up by azure-monitor-opentelemetry's
              // configure_azure_monitor() at process start (see
              // cms/observability.py).  When unset (e.g. local dev,
              // docker-compose) telemetry export is silently disabled.
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              secretRef: 'app-insights-connection-string'
            }
            {
              // Azure OpenAI endpoint for the Assistant feature.
              // Empty string in environments that haven't opted into
              // the feature; the runtime treats empty as "Assistant
              // disabled" so non-opted-in envs ship with the same
              // image but the feature stays dark.
              name: 'AGORA_CMS_AZURE_OPENAI_ENDPOINT'
              value: azureOpenAIEndpoint
            }
            {
              name: 'AGORA_CMS_AZURE_OPENAI_DEPLOYMENT'
              value: azureOpenAIDeployment
            }
            {
              // Tag every emitted record so we can distinguish prod from
              // dev/staging in shared workbooks and KQL.
              name: 'OTEL_RESOURCE_ATTRIBUTES'
              value: 'service.name=agora-cms,service.namespace=agora,deployment.environment=${environmentName}'
            }
          ]
          volumeMounts: [
            {
              volumeName: 'transcode-workspace'
              mountPath: '/opt/agora-cms/assets'
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'transcode-workspace'
          storageName: transcodeStorage.name
          storageType: 'AzureFile'
        }
      ]
      scale: {
        minReplicas: 2
        // Multi-replica safe as of issue #344 completion (April 2026).
        // All singleton state has been moved to DB-backed or leader-gated
        // paths: device presence, confirmed-playing, missed-schedule state,
        // temp-alert state, scheduler lease, log-request flow, WPS
        // transport fan-out.  Pinned to N=2 rather than letting autoscale
        // decide so the multi-replica paths are always exercised and we
        // learn about regressions before they surface at higher scale.
        // Next step (post-stability): lift maxReplicas and gate scaling
        // on CPU/HTTP metrics.
        maxReplicas: 2
      }
    }
  }
}

// ── MCP Server Container App ──
resource mcpApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: mcpAppName
  location: location
  tags: tags
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    managedEnvironmentId: containerAppsEnv.id
    configuration: {
      // Blue/green: see cmsApp comment above.
      activeRevisionsMode: 'Multiple'
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
        traffic: !empty(previousMcpRevisionName) ? [
          {
            revisionName: previousMcpRevisionName
            weight: 100
          }
        ] : [
          {
            latestRevision: true
            weight: 100
          }
        ]
      }
      secrets: []
    }
    template: {
      revisionSuffix: mcpRevisionSuffix
      containers: [
        {
          name: 'mcp'
          image: mcpImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            {
              name: 'CMS_BASE_URL'
              value: 'http://${cmsAppName}'
            }
            {
              name: 'AZURE_KEYVAULT_URI'
              value: keyVaultUri
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// ── Worker Container Apps Job (queue-triggered transcoding) ──
resource workerJob 'Microsoft.App/jobs@2024-03-01' = {
  name: workerJobName
  location: location
  tags: tags
  properties: {
    environmentId: containerAppsEnv.id
    configuration: {
      triggerType: 'Event'
      replicaTimeout: 7200 // 2 hour max per execution — SIGTERM handler in worker marks FAILED within 30s grace
      replicaRetryLimit: 1
      eventTriggerConfig: {
        replicaCompletionCount: 1
        parallelism: 1
        scale: {
          minExecutions: 0
          maxExecutions: 3
          pollingInterval: 10
          rules: [
            {
              name: 'transcode-queue'
              type: 'azure-queue'
              metadata: {
                queueName: 'transcode-jobs'
                queueLength: '1'
                accountName: storageAccountName
                connectionFromEnv: 'AZURE_STORAGE_CONNECTION_STRING'
              }
              auth: [
                {
                  secretRef: 'storage-connection-string'
                  triggerParameter: 'connection'
                }
              ]
            }
          ]
        }
      }
      secrets: [
        {
          name: 'worker-database-url'
          value: cmsDatabaseUrl
        }
        {
          name: 'storage-connection-string'
          value: storageConnectionString
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'worker'
          image: workerImage
          resources: {
            cpu: json(workerCpu)
            memory: workerMemory
          }
          env: [
            {
              name: 'AGORA_CMS_DATABASE_URL'
              secretRef: 'worker-database-url'
            }
            {
              name: 'AGORA_CMS_WORKER_MODE'
              value: 'queue'
            }
            {
              name: 'AGORA_CMS_STORAGE_BACKEND'
              value: 'azure'
            }
            {
              name: 'AGORA_CMS_AZURE_STORAGE_CONNECTION_STRING'
              secretRef: 'storage-connection-string'
            }
            {
              name: 'AZURE_STORAGE_CONNECTION_STRING'
              secretRef: 'storage-connection-string'
            }
          ]
          volumeMounts: [
            {
              volumeName: 'transcode-workspace'
              mountPath: '/opt/agora-cms/assets'
            }
          ]
        }
      ]
      volumes: [
        {
          name: 'transcode-workspace'
          storageName: transcodeStorage.name
          storageType: 'AzureFile'
        }
      ]
    }
  }
}

output cmsAppFqdn string = cmsApp.properties.configuration.ingress.fqdn
output cmsAppUrl string = 'https://${cmsApp.properties.configuration.ingress.fqdn}'
output mcpAppFqdn string = mcpApp.properties.configuration.ingress.fqdn
output mcpAppUrl string = 'https://${mcpApp.properties.configuration.ingress.fqdn}'
output environmentDefaultDomain string = containerAppsEnv.properties.defaultDomain
output cmsLatestRevisionName string = cmsApp.properties.latestRevisionName
output mcpLatestRevisionName string = mcpApp.properties.latestRevisionName
output environmentId string = containerAppsEnv.id
output cmsPrincipalId string = cmsApp.identity.principalId
output mcpPrincipalId string = mcpApp.identity.principalId
output logAnalyticsWorkspaceId string = logAnalytics.id
output appInsightsId string = appInsights.id
output appInsightsName string = appInsights.name
