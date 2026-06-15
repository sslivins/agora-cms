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

@description('GitHub personal access token used by the "Report an issue" feature to file issues against the configured repo. When empty, the report-issue button is hidden.')
@secure()
param githubIssuesToken string = ''

@description('Public CMS URL override (e.g. https://agora.example.com, no trailing slash). When non-empty, used for AGORA_CMS_BASE_URL on the CMS container -- needed when a custom domain fronts the app so invite/setup-account email links and the wss://host/ws/device URL baked into provisioned Pis point at the real public hostname. When empty, bicep auto-derives the Azure default-domain URL.')
param cmsBaseUrlOverride string = ''

@description('Device transport mode: "wps" (multi-replica safe, routes via Azure Web PubSub — provisioned and wired by this template) or "local" (direct CMS→device websockets, single-replica only — skips the WPS resource entirely).')
@allowed(['wps', 'local'])
param deviceTransport string = 'wps'

@description('Web PubSub SKU when deviceTransport=wps. Free_F1 covers hundreds of concurrent devices; bump to Standard_S1 for >1k connections, custom domains, or revenue-bearing prod.')
@allowed(['Free_F1', 'Standard_S1'])
param webPubSubSku string = 'Free_F1'

@description('CMS container image (e.g., ghcr.io/sslivins/agora-cms:1.12.34). Defaults to the latest GHCR tag.')
param cmsImage string = 'ghcr.io/sslivins/agora-cms:latest'

@description('MCP server container image (e.g., ghcr.io/sslivins/agora-cms-mcp:1.12.34). Defaults to the latest GHCR tag.')
param mcpImage string = 'ghcr.io/sslivins/agora-cms-mcp:latest'

// ── Blue/green deploy controls ──
@description('Per-deploy revision suffix for the CMS Container App (e.g. "v1-12-34"). The CD workflow generates this from the version. New revision lands at 0% traffic; workflow flips traffic post-verify.')
param cmsRevisionSuffix string = ''

@description('Name of the existing CMS revision that should keep 100% traffic during this deploy. CD workflow queries Azure for the current latestReadyRevisionName. Empty for bootstrap.')
param previousCmsRevisionName string = ''

@description('Per-deploy revision suffix for the MCP Container App. See cmsRevisionSuffix.')
param mcpRevisionSuffix string = ''

@description('Name of the existing MCP revision that should keep 100% traffic during this deploy. See previousCmsRevisionName.')
param previousMcpRevisionName string = ''

@description('Worker container image (e.g., ghcr.io/sslivins/agora-worker:1.12.34). Defaults to the latest GHCR tag.')
param workerImage string = 'ghcr.io/sslivins/agora-worker:latest'

@description('Worker container CPU cores (Container Apps Job: up to 4.0)')
param workerCpu string = '4.0'

@description('Worker container memory (must be 2× CPU, e.g. 4.0→8Gi)')
param workerMemory string = '8Gi'

@description('Deploy RBAC role assignments (requires Owner/UAA). Set false for CD pipelines with Contributor-only access.')
param deployRoleAssignments bool = true

@description('CMS container CPU cores (Consumption tier: 0.25–2.0 in 0.25 steps)')
@allowed(['0.25', '0.5', '0.75', '1.0', '1.25', '1.5', '1.75', '2.0'])
param cmsCpu string = '1.0'

@description('CMS container memory (must be 2× CPU, e.g. 0.5→1Gi, 1.0→2Gi, 2.0→4Gi)')
param cmsMemory string = '2Gi'

@description('Maximum number of CMS container-app replicas. Sets the autoscale ceiling AND drives the Postgres max_connections formula (postgres.bicep). Pinned to 2 today.')
@minValue(1)
param cmsMaxReplicas int = 2

@description('When true, Bicep manages Postgres max_connections via the replica-count formula (base + perReplica*cmsMaxReplicas). When false (default, all envs except dev), max_connections stays at the B1ms default (50). Enable per-env in its .bicepparam.')
param manageMaxConnections bool = false

@description('Object ID of the Azure AD user/principal for Key Vault admin access')
param adminPrincipalId string

@description('Email recipient for telemetry alerts (CMS 5xx, latency, dependency failures, exceptions). Empty disables the alerts module entirely (e.g. for dev environments where pages are noise).')
param alertEmail string = ''

@description('When true, replace the legacy request-count CMS heartbeat alert with an Application Insights standard availability test against /healthz. Recommended for low-traffic / idle-friendly environments (dev, fresh prod) where the OTel SDK filters probe traffic and the legacy alert produces false positives. Currently opt-in; will be flipped to default-true after dev validation.')
param useSyntheticHeartbeat bool = false

// ── Assistant feature (Azure OpenAI) ──
@description('When true, deploy an Azure OpenAI account + chat model deployment and wire it to the CMS container app. Required by the Assistant feature; safe to leave false in environments that are not opted in yet — the CMS treats missing endpoint env vars as "feature disabled" at runtime.')
param deployAzureOpenAI bool = false

@description('Azure region for the Azure OpenAI account when deployAzureOpenAI=true. Defaults to the RG location; override when the RG region has no quota for the requested model. westus has GPT-4o; eastus2 has best GPT-5 quota.')
param azureOpenAIRegion string = ''

@description('Chat model to deploy under the "chat" deployment name on the Azure OpenAI account. Only used when deployAzureOpenAI=true.')
@allowed(['gpt-4o', 'gpt-4o-mini', 'gpt-4.1', 'gpt-4.1-mini'])
param azureOpenAIChatModel string = 'gpt-4o'

@description('Chat model version pinned for the "chat" deployment. Validate against the Azure OpenAI model-versions doc for the chosen azureOpenAIRegion.')
param azureOpenAIChatModelVersion string = '2024-11-20'

@description('TPM capacity for the chat deployment, in thousands (30 = 30k TPM). Phase-1 default of 30 is generous for a single-org pilot.')
param azureOpenAIChatCapacity int = 30

var tags = {
  project: 'agora-cms'
  managedBy: 'bicep'
}

// Resource names derived from prefix
var vnetName = '${prefix}-vnet'
var postgresServerName = '${prefix}-pg'
var storageAccountName = take(replace('${prefix}stg${uniqueString(resourceGroup().id)}', '-', ''), 24)
var keyVaultName = '${prefix}-vault'
var containerAppsEnvName = '${prefix}-env'
var cmsAppName = '${prefix}-cms'
var mcpAppName = '${prefix}-mcp'
var workerJobName = '${prefix}-worker'
var webPubSubName = '${prefix}-cms-wps'
var azureOpenAIAccountName = '${prefix}-aoai'

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
    // max_connections replica-count formula (no-op unless manageMaxConnections=true).
    manageMaxConnections: manageMaxConnections
    cmsMaxReplicas: cmsMaxReplicas
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

// ── Key Vault ──
module keyVault 'modules/keyVault.bicep' = {
  name: 'keyVault'
  params: {
    location: location
    keyVaultName: keyVaultName
    adminPrincipalId: adminPrincipalId
    deployRoleAssignments: deployRoleAssignments
    tags: tags
  }
}

// ── Web PubSub (device transport) ──
// Provisioned only in WPS mode. The hub itself is created below,
// AFTER containerApps, so its event-handler URL can reference the
// env's generated defaultDomain. Splitting resource and hub into
// two modules keeps a clean single-pass deploy with no manual
// `az webpubsub` steps.
module webPubSub 'modules/webPubSub.bicep' = if (deviceTransport == 'wps') {
  name: 'webPubSub'
  params: {
    location: location
    webPubSubName: webPubSubName
    skuName: webPubSubSku
    tags: tags
  }
}

// ── Azure OpenAI (Assistant feature backend) ──
// Optional: only deployed when deployAzureOpenAI=true. Account
// region defaults to the RG region; override via azureOpenAIRegion
// when the RG region lacks quota for the requested model.
module azureOpenAI 'modules/azureOpenAI.bicep' = if (deployAzureOpenAI) {
  name: 'azureOpenAI'
  params: {
    location: empty(azureOpenAIRegion) ? location : azureOpenAIRegion
    accountName: azureOpenAIAccountName
    chatModel: azureOpenAIChatModel
    chatModelVersion: azureOpenAIChatModelVersion
    chatModelCapacity: azureOpenAIChatCapacity
    tags: tags
  }
}

// ── Build database connection string ──
// URL-encode the '@' in the password so asyncpg parses the URL correctly
var encodedPassword = replace(postgresAdminPassword, '@', '%40')
var databaseUrl = 'postgresql+asyncpg://${postgresAdminLogin}:${encodedPassword}@${postgres.outputs.serverFqdn}:5432/${postgres.outputs.databaseName}?ssl=require'

// ── Container Apps (CMS + MCP) ──
module containerApps 'modules/containerApps.bicep' = {
  name: 'containerApps'
  params: {
    location: location
    environmentName: containerAppsEnvName
    containerAppsSubnetId: networking.outputs.containerAppsSubnetId
    containerAppsSubnetCidr: networking.outputs.containerAppsSubnetCidr
    tags: tags

    // CMS
    cmsAppName: cmsAppName
    cmsImage: cmsImage
    cmsCpu: cmsCpu
    cmsMemory: cmsMemory
    cmsMaxReplicas: cmsMaxReplicas
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

    // MCP
    mcpAppName: mcpAppName
    mcpImage: mcpImage

    // Blue/green revision controls
    cmsRevisionSuffix: cmsRevisionSuffix
    previousCmsRevisionName: previousCmsRevisionName
    mcpRevisionSuffix: mcpRevisionSuffix
    previousMcpRevisionName: previousMcpRevisionName

    // Worker Job
    workerJobName: workerJobName
    workerImage: workerImage
    workerCpu: workerCpu
    workerMemory: workerMemory

    // Key Vault (service key exchange)
    keyVaultUri: keyVault.outputs.keyVaultUri

    // Device transport (Azure Web PubSub)
    // Connection string flows directly from the WPS module's listKeys()
    // output — no GH secret, no manual key plumbing. Empty string in
    // local mode (containerApps tolerates an empty value).
    wpsConnectionString: deviceTransport == 'wps' ? webPubSub.outputs.connectionString : ''
    deviceTransport: deviceTransport

    // Report-issue feature (GitHub)
    githubIssuesToken: githubIssuesToken

    // Public CMS URL override (custom domain). Propagated to AGORA_CMS_BASE_URL.
    cmsBaseUrlOverride: cmsBaseUrlOverride

    // Azure OpenAI (Assistant feature). Empty when not deployed —
    // CMS treats empty endpoint as "feature disabled" at runtime.
    azureOpenAIEndpoint: deployAzureOpenAI ? azureOpenAI.outputs.endpoint : ''
    azureOpenAIDeployment: deployAzureOpenAI ? azureOpenAI.outputs.deploymentName : ''
    azureOpenAIModel: deployAzureOpenAI ? azureOpenAIChatModel : ''
  }
}

// ── Azure OpenAI RBAC ──
// Grant the CMS container app's managed identity the
// 'Cognitive Services OpenAI User' role on the AOAI account so it
// can call chat.completions with DefaultAzureCredential, no keys
// required. Role guid 5e0bd9bd-7b93-4f28-af87-19fc36ad61bd is the
// built-in 'Cognitive Services OpenAI User' definition.
resource existingAzureOpenAI 'Microsoft.CognitiveServices/accounts@2024-10-01' existing = if (deployAzureOpenAI) {
  name: azureOpenAIAccountName
}

resource cmsAzureOpenAIRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployAzureOpenAI && deployRoleAssignments) {
  name: guid(existingAzureOpenAI.id, cmsAppName, '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
  scope: existingAzureOpenAI
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '5e0bd9bd-7b93-4f28-af87-19fc36ad61bd')
    principalId: containerApps.outputs.cmsPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ── Key Vault RBAC for Container Apps managed identities ──
resource existingKeyVault 'Microsoft.KeyVault/vaults@2023-07-01' existing = {
  name: keyVaultName
}

// CMS: Key Vault Secrets Officer (read + write service key)
resource cmsKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments) {
  name: guid(existingKeyVault.id, cmsAppName, 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
  scope: existingKeyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', 'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')
    principalId: containerApps.outputs.cmsPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// MCP: Key Vault Secrets User (read-only service key)
resource mcpKeyVaultRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = if (deployRoleAssignments) {
  name: guid(existingKeyVault.id, mcpAppName, '4633458b-17de-408a-b874-0445c86b69e6')
  scope: existingKeyVault
  properties: {
    roleDefinitionId: subscriptionResourceId('Microsoft.Authorization/roleDefinitions', '4633458b-17de-408a-b874-0445c86b69e6')
    principalId: containerApps.outputs.mcpPrincipalId
    principalType: 'ServicePrincipal'
  }
}

// ── Web PubSub hub ("agora") ──
// Deployed AFTER containerApps so the event-handler URL can use the
// CMS's actual FQDN (built from cmsAppName + the env's generated
// defaultDomain). Without this hub, Azure accepts device WSS but
// never POSTs sys.connected to the CMS webhook, so every device
// shows offline forever.
module webPubSubHub 'modules/webPubSubHub.bicep' = if (deviceTransport == 'wps') {
  name: 'webPubSubHub'
  params: {
    webPubSubName: webPubSub.outputs.name
    hubName: 'agora'
    cmsFqdn: '${cmsAppName}.${containerApps.outputs.environmentDefaultDomain}'
  }
}

// ── Telemetry: alerts + workbook (Phase 0 / A1.5) ──
module alerts 'modules/alerts.bicep' = {
  name: 'alerts'
  params: {
    location: location
    namePrefix: prefix
    appInsightsId: containerApps.outputs.appInsightsId
    appInsightsName: containerApps.outputs.appInsightsName
    alertEmail: alertEmail
    tags: tags
    cmsFqdn: containerApps.outputs.cmsAppFqdn
    useSyntheticHeartbeat: useSyntheticHeartbeat
  }
}

// ── Outputs ──
output cmsUrl string = containerApps.outputs.cmsAppFqdn
output mcpUrl string = containerApps.outputs.mcpAppFqdn
output environmentDefaultDomain string = containerApps.outputs.environmentDefaultDomain
output cmsLatestRevisionName string = containerApps.outputs.cmsLatestRevisionName
output mcpLatestRevisionName string = containerApps.outputs.mcpLatestRevisionName
output postgresServerFqdn string = postgres.outputs.serverFqdn
output keyVaultUri string = keyVault.outputs.keyVaultUri
output storageAccountName string = storage.outputs.storageAccountName
output azureOpenAIEndpoint string = deployAzureOpenAI ? azureOpenAI.outputs.endpoint : ''
output azureOpenAIDeployment string = deployAzureOpenAI ? azureOpenAI.outputs.deploymentName : ''
