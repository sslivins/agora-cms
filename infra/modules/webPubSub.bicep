// ──────────────────────────────────────────────────────────────
// webPubSub.bicep — Azure Web PubSub resource
//
// Provides the routing fabric for fanning device events across
// multiple CMS replicas (AGORA_CMS_DEVICE_TRANSPORT=wps mode).
// The "agora" hub itself is created in webPubSubHub.bicep, after
// the Container Apps env exists — the hub's event-handler URL
// points at the CMS, so the two have to be sequenced.
//
// Auth model: connection-string only (disableLocalAuth=false). The
// CMS uses the access key to mint per-device JWT client tokens
// (cms.services.wps_transport.get_client_access_token); devices
// connect with that JWT, so the hub itself denies anonymous.
// ──────────────────────────────────────────────────────────────

param location string
param webPubSubName string

@allowed(['Free_F1', 'Standard_S1'])
param skuName string = 'Free_F1'

param skuCapacity int = 1
param tags object = {}

resource webPubSub 'Microsoft.SignalRService/webPubSub@2024-03-01' = {
  name: webPubSubName
  location: location
  tags: tags
  sku: {
    name: skuName
    capacity: skuCapacity
  }
  properties: {
    disableLocalAuth: false
    publicNetworkAccess: 'Enabled'
    tls: {
      clientCertEnabled: false
    }
  }
}

output name string = webPubSub.name
output id string = webPubSub.id
output hostName string = webPubSub.properties.hostName

@secure()
output connectionString string = webPubSub.listKeys().primaryConnectionString
