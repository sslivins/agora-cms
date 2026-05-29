// ──────────────────────────────────────────────────────────────
// azureOpenAI.bicep — Azure OpenAI account + chat model deployment
//
// Provisions the LLM backend for the in-CMS Assistant feature
// (PR series starting with "feat: assistant infra"). Deployed only
// when main.bicep's deployAzureOpenAI=true; otherwise omitted
// entirely so non-opted-in environments (prod) carry zero cost.
//
// Auth model: managed identity only (disableLocalAuth=true). The
// CMS container app's system-assigned identity is granted the
// 'Cognitive Services OpenAI User' role at the account scope in
// main.bicep. No API keys are minted, stored, or rotated.
//
// Model deployment: a single 'chat' deployment of the configured
// model (default gpt-4o). One deployment is enough for Phase 1 —
// add additional deployments here when we need fallback models or
// per-tier routing.
//
// Quota gotcha: TPM is per-subscription-per-region-per-model. If
// the deployment fails with InsufficientQuota, file an Azure
// support quota-increase request for `OpenAI.Standard.{model}` in
// `azureOpenAIRegion` before retrying.
// ──────────────────────────────────────────────────────────────

@description('Azure region for the Azure OpenAI account. May differ from the parent RG location to follow model quota. westus has GPT-4o; eastus2 has best GPT-5 quota.')
param location string

@description('Name of the Azure OpenAI account.')
param accountName string

@description('Model family to deploy as the "chat" deployment.')
@allowed(['gpt-4o', 'gpt-4o-mini', 'gpt-4.1', 'gpt-4.1-mini'])
param chatModel string = 'gpt-4o'

@description('Model version to pin. Check the Azure OpenAI model-versions doc for currently-supported values per region.')
param chatModelVersion string = '2024-11-20'

@description('Tokens-per-minute capacity (in thousands). 30 = 30k TPM, plenty for Phase 1 / single-org pilot. Bump when monthly budget caps stop biting first.')
param chatModelCapacity int = 30

param tags object = {}

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'OpenAI'
  sku: {
    name: 'S0'
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    // Force managed-identity auth so we never have to plumb API
    // keys through Key Vault. CMS uses azure-identity's
    // DefaultAzureCredential at runtime.
    disableLocalAuth: true
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
  }
}

resource chatDeployment 'Microsoft.CognitiveServices/accounts/deployments@2024-10-01' = {
  parent: account
  name: 'chat'
  sku: {
    // Standard = pay-per-token, regional. GlobalStandard would
    // spread load across regions; not needed at Phase 1 volumes
    // and complicates quota accounting.
    name: 'Standard'
    capacity: chatModelCapacity
  }
  properties: {
    model: {
      format: 'OpenAI'
      name: chatModel
      version: chatModelVersion
    }
    // Default content-filter policy is fine for an internal admin
    // assistant; revisit if we ever expose this to end-users.
    raiPolicyName: 'Microsoft.DefaultV2'
  }
}

output accountName string = account.name
output accountId string = account.id
output endpoint string = account.properties.endpoint
output deploymentName string = chatDeployment.name
