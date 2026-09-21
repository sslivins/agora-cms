// ──────────────────────────────────────────────────────────────
// azureSpeech.bicep — Azure AI Speech account
//
// Provisions the backend used by Voice Announcements synthesis. Deployed
// only when main.bicep's deployAzureSpeech=true so non-opted-in
// environments carry zero cost.
//
// Auth model: managed identity only (disableLocalAuth=true). The CMS
// container app and worker job identities are granted the
// 'Cognitive Services Speech User' role in main.bicep. No API keys are
// minted, stored, or rotated.
// ──────────────────────────────────────────────────────────────

@description('Azure region for the Azure AI Speech account. May differ from the parent RG location to follow voice/model availability.')
param location string

@description('Name of the Azure AI Speech account.')
param accountName string

@description('SKU for the Azure AI Speech account. S0 is required for neural/MAI voices.')
param sku string = 'S0'

param tags object = {}

resource account 'Microsoft.CognitiveServices/accounts@2024-10-01' = {
  name: accountName
  location: location
  tags: tags
  kind: 'SpeechServices'
  sku: {
    name: sku
  }
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    disableLocalAuth: true
    customSubDomainName: accountName
    publicNetworkAccess: 'Enabled'
  }
}

output accountName string = account.name
output accountId string = account.id
output endpoint string = account.properties.endpoint
output location string = account.location
