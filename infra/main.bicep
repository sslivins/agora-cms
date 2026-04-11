// ──────────────────────────────────────────────────────────────
// main.bicep — Agora CMS Azure Infrastructure
//
// Deploys all resources for the Agora CMS:
//   - VNet with subnets (Container Apps + PostgreSQL)
//   - Azure Database for PostgreSQL Flexible Server
//   - Storage Account (Azure Files + Blob containers)
//   - Azure Container Registry
//   - Azure Key Vault
//   - Container Apps (CMS + MCP server)
//
// Usage:
//   az deployment group create \
//     --resource-group agora-cms-rg \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters/prod.bicepparam
// ──────────────────────────────────────────────────────────────

@description('Azure region for all resources')
param location string = resourceGroup().location

@description('Unique prefix for resource names (lowercase, no special chars)')
@minLength(3)
@maxLength(12)
param prefix string

@description('PostgreSQL administrator login name')
param postgresAdminLogin string = 'agoraadmin'

@description('PostgreSQL administrator password')
@secure()
param postgresAdminPassword string

@description('CMS application secret key (for session/JWT signing)')
@secure()
param cmsSecretKey string

@description('CMS initial admin username')
param cmsAdminUsername string = 'admin'

@description('CMS initial admin password')
@secure()
param cmsAdminPassword string

@description('CMS container image (e.g., agoracr.azurecr.io/agora-cms:latest)')
param cmsImage string = ''

@description('MCP server container image (e.g., agoracr.azurecr.io/agora-cms-mcp:latest)')
param mcpImage string = ''

@description('API key for MCP server to authenticate with CMS (auto-generated post-deploy if empty)')
@secure()
param mcpApiKey string = 'placeholder-will-be-replaced'

@description('CMS container CPU cores (Consumption tier: 0.25–2.0 in 0.25 steps)')
@allowed(['0.25', '0.5', '0.75', '1.0', '1.25', '1.5', '1.75', '2.0'])
param cmsCpu string = '1.0'

@description('CMS container memory (must be 2× CPU, e.g. 0.5→1Gi, 1.0→2Gi, 2.0→4Gi)')
param cmsMemory string = '2Gi'

@description('Object ID of the Azure AD user/principal for Key Vault admin access')
param adminPrincipalId string

var tags = {
  project: 'agora-cms'
  managedBy: 'bicep'
}

// Resource names derived from prefix
var vnetName = '${prefix}-vnet'
var postgresServerName = '${prefix}-pg'
var storageAccountName = take(replace('${prefix}stg${uniqueString(resourceGroup().id)}', '-', ''), 24)
var acrName = replace('${prefix}acr', '-', '')
var keyVaultName = '${prefix}-kv'
var containerAppsEnvName = '${prefix}-env'
var cmsAppName = '${prefix}-cms'
var mcpAppName = '${prefix}-mcp'

// ── Networking ──
module networking 'modules/networking.bicep' = {
  name: 'networking'
  params: {
    location: location
    vnetName: vnetName
    tags: tags
  }
}

// ── PostgreSQL ──
module postgres 'modules/postgres.bicep' = {
  name: 'postgres'
  params: {
    location: location
    serverName: postgresServerName
    administratorLogin: postgresAdminLogin
    administratorPassword: postgresAdminPassword
    postgresSubnetId: networking.outputs.postgresSubnetId
    privateDnsZoneId: networking.outputs.privateDnsZoneId
    tags: tags
  }
}

// ── Storage (Azure Files + Blob) ──
module storage 'modules/storage.bicep' = {
  name: 'storage'
  params: {
    location: location
    storageAccountName: storageAccountName
    tags: tags
  }
}

// ── Container Registry ──
module acr 'modules/acr.bicep' = {
  name: 'acr'
  params: {
    location: location
    acrName: acrName
    tags: tags
  }
}

// ── Key Vault ──
module keyVault 'modules/keyVault.bicep' = {
  name: 'keyVault'
  params: {
    location: location
    keyVaultName: keyVaultName
    adminPrincipalId: adminPrincipalId
    tags: tags
  }
}

// ── Build database connection string ──
var databaseUrl = 'postgresql+asyncpg://${postgresAdminLogin}:${postgresAdminPassword}@${postgres.outputs.serverFqdn}:5432/${postgres.outputs.databaseName}?ssl=require'

// ── Determine container images ──
// Use provided images or default to ACR-based names
var resolvedCmsImage = !empty(cmsImage) ? cmsImage : '${acr.outputs.acrLoginServer}/agora-cms:latest'
var resolvedMcpImage = !empty(mcpImage) ? mcpImage : '${acr.outputs.acrLoginServer}/agora-cms-mcp:latest'

// ── Container Apps (CMS + MCP) ──
module containerApps 'modules/containerApps.bicep' = {
  name: 'containerApps'
  params: {
    location: location
    environmentName: containerAppsEnvName
    containerAppsSubnetId: networking.outputs.containerAppsSubnetId
    tags: tags

    // CMS
    cmsAppName: cmsAppName
    cmsImage: resolvedCmsImage
    cmsCpu: cmsCpu
    cmsMemory: cmsMemory
    cmsDatabaseUrl: databaseUrl
    cmsSecretKey: cmsSecretKey
    cmsAdminUsername: cmsAdminUsername
    cmsAdminPassword: cmsAdminPassword

    // Storage
    storageConnectionString: 'DefaultEndpointsProtocol=https;AccountName=${storage.outputs.storageAccountName};AccountKey=${storage.outputs.storageAccountKey};EndpointSuffix=core.windows.net'
    storageBlobEndpoint: storage.outputs.blobEndpoint
    storageAccountName: storage.outputs.storageAccountName
    storageAccountKey: storage.outputs.storageAccountKey
    transcodeShareName: storage.outputs.transcodeShareName

    // ACR
    acrLoginServer: acr.outputs.acrLoginServer
    acrUsername: acr.outputs.acrUsername
    acrPassword: acr.outputs.acrPassword

    // MCP
    mcpAppName: mcpAppName
    mcpImage: resolvedMcpImage
    mcpApiKey: mcpApiKey
  }
}

// ── Outputs ──
output cmsUrl string = containerApps.outputs.cmsAppFqdn
output mcpUrl string = containerApps.outputs.mcpAppFqdn
output acrLoginServer string = acr.outputs.acrLoginServer
output postgresServerFqdn string = postgres.outputs.serverFqdn
output keyVaultUri string = keyVault.outputs.keyVaultUri
output storageAccountName string = storage.outputs.storageAccountName
