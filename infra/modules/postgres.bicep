// ──────────────────────────────────────────────────────────────
// postgres.bicep — Azure Database for PostgreSQL Flexible Server
// ──────────────────────────────────────────────────────────────

param location string
param serverName string
param administratorLogin string
@secure()
param administratorPassword string
param databaseName string = 'agora_cms'
param postgresSubnetId string
param privateDnsZoneId string
param tags object = {}

resource postgresServer 'Microsoft.DBforPostgreSQL/flexibleServers@2024-08-01' = {
  name: serverName
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: administratorLogin
    administratorLoginPassword: administratorPassword
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    network: {
      delegatedSubnetResourceId: postgresSubnetId
      privateDnsZoneArmResourceId: privateDnsZoneId
    }
    highAvailability: {
      mode: 'Disabled'
    }
  }
}

resource database 'Microsoft.DBforPostgreSQL/flexibleServers/databases@2024-08-01' = {
  parent: postgresServer
  name: databaseName
  properties: {
    charset: 'UTF8'
    collation: 'en_US.utf8'
  }
}

// Require SSL connections
resource sslConfig 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgresServer
  name: 'require_secure_transport'
  properties: {
    value: 'on'
    source: 'user-override'
  }
}

// Raise max_connections from the B1ms default (50) to its supported ceiling.
// 2 CMS replicas (5+5 pool each) + worker LISTEN + concurrent imager jobs
// can transiently exceed 50 under load. 85 is the documented maximum for
// the Burstable B1ms SKU and gives generous headroom for CI seed scripts
// and ad-hoc admin connections. See incident at 2026-05-06T13-14Z.
resource maxConnectionsConfig 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgresServer
  name: 'max_connections'
  properties: {
    value: '85'
    source: 'user-override'
  }
}

output serverFqdn string = postgresServer.properties.fullyQualifiedDomainName
output serverName string = postgresServer.name
output databaseName string = database.name
