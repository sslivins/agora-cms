// ──────────────────────────────────────────────────────────────
// webPubSubHub.bicep — "agora" hub + CMS event handler
//
// Without this child resource, devices can open WSS to the WPS
// resource but Azure has no event handler to fire — the CMS
// webhook (/internal/wps/events) never sees sys.connected /
// sys.disconnected, so every device shows offline forever.
//
// This module is intentionally split from webPubSub.bicep so the
// hub can deploy AFTER the Container Apps env exists (the
// event-handler URL embeds the env's generated defaultDomain).
// Bicep handles the dependency implicitly via the cmsFqdn input.
//
// Hub name MUST match cms.config.AppConfig.wps_hub (currently
// "agora"). Keep both sides aligned when changing.
// ──────────────────────────────────────────────────────────────

param webPubSubName string

@description('Hub name — must match cms.config.AppConfig.wps_hub.')
param hubName string = 'agora'

@description('Public FQDN of the CMS Container App, e.g. "agoragw-cms.yellowdune-84efd1e7.westus2.azurecontainerapps.io". Used to build the event-handler URL Azure POSTs CloudEvents to.')
param cmsFqdn string

resource webPubSub 'Microsoft.SignalRService/webPubSub@2024-03-01' existing = {
  name: webPubSubName
}

resource hub 'Microsoft.SignalRService/webPubSub/hubs@2024-03-01' = {
  parent: webPubSub
  name: hubName
  properties: {
    // Devices present a CMS-minted JWT in the connect_token query
    // param, so anonymous connections must be refused outright.
    anonymousConnectPolicy: 'deny'
    eventHandlers: [
      {
        urlTemplate: 'https://${cmsFqdn}/internal/wps/events'
        userEventPattern: '*'
        systemEvents: [
          'connected'
          'disconnected'
        ]
        // No auth header — Azure signs the request with the WPS
        // access key (the same one in the connection string the
        // CMS holds), and the webhook verifies via ce-signature.
      }
    ]
  }
}

output hubName string = hub.name
output hubId string = hub.id
