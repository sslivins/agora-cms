// ──────────────────────────────────────────────────────────────
// acr.bicep — Azure Container Registry (Basic tier)
// ──────────────────────────────────────────────────────────────

param location string
param acrName string
param tags object = {}

resource containerRegistry 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' = {
  name: acrName
  location: location
  tags: tags
  sku: {
    name: 'Basic'
  }
  properties: {
    adminUserEnabled: true
  }
}

output acrLoginServer string = containerRegistry.properties.loginServer
output acrName string = containerRegistry.name
output acrId string = containerRegistry.id
