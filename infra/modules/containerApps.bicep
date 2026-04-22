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

// ── ACR credentials ──
param acrLoginServer string
@secure()
param acrUsername string
@secure()
param acrPassword string

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
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto' // supports both HTTP and WebSocket
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          username: acrUsername
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: [
        {
          name: 'acr-password'
          value: acrPassword
        }
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
      ]
    }
    template: {
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
        minReplicas: 1
        // Pinned to 1 until multi-replica architecture lands (issue #344).
        // CMS is not multi-replica safe today: in-memory device manager,
        // scheduler caches, singleton background loops, and the
        // `_upgrading` guard all assume a single process. Scaling > 1
        // silently drops device traffic and double-schedules work. See
        // docs/multi-replica-architecture.md for the staged plan to
        // lift this restriction.
        maxReplicas: 1
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
      ingress: {
        external: true
        targetPort: 8000
        transport: 'auto'
        allowInsecure: false
      }
      registries: [
        {
          server: acrLoginServer
          username: acrUsername
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: [
        {
          name: 'acr-password'
          value: acrPassword
        }
      ]
    }
    template: {
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
      registries: [
        {
          server: acrLoginServer
          username: acrUsername
          passwordSecretRef: 'acr-password'
        }
      ]
      secrets: [
        {
          name: 'acr-password'
          value: acrPassword
        }
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
output environmentId string = containerAppsEnv.id
output cmsPrincipalId string = cmsApp.identity.principalId
output mcpPrincipalId string = mcpApp.identity.principalId
