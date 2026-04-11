// ──────────────────────────────────────────────────────────────
// containerApps.bicep — Container Apps Environment + CMS + MCP apps
// ──────────────────────────────────────────────────────────────

param location string
param environmentName string
param containerAppsSubnetId string
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
@secure()
param mcpApiKey string

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
        maxReplicas: 3
      }
    }
  }
}

// ── MCP Server Container App ──
resource mcpApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: mcpAppName
  location: location
  tags: tags
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
        {
          name: 'mcp-api-key'
          value: mcpApiKey
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
              name: 'CMS_API_KEY'
              secretRef: 'mcp-api-key'
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

output cmsAppFqdn string = cmsApp.properties.configuration.ingress.fqdn
output cmsAppUrl string = 'https://${cmsApp.properties.configuration.ingress.fqdn}'
output mcpAppFqdn string = mcpApp.properties.configuration.ingress.fqdn
output mcpAppUrl string = 'https://${mcpApp.properties.configuration.ingress.fqdn}'
output environmentId string = containerAppsEnv.id
