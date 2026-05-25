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

// Allow-list pg_trgm so migration 0030 (asset-library trigram indexes) can
// CREATE EXTENSION pg_trgm at app startup. On Azure PG Flex, even an admin
// role cannot create an extension that isn't named here -- the statement
// fails with "extension X is not allow-listed for azure.extensions". The
// 2026-05-25 prod incident (revision agoracms-cms--v1-37-255 crash-looping
// on /healthz for ~50 min) was exactly this: the parameter was unset, so
// init_db()'s alembic upgrade hung the container at "Running upgrade 0029
// -> 0030" without surfacing the error before Azure SIGKILL'd it.
resource extensionsConfig 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgresServer
  name: 'azure.extensions'
  properties: {
    value: 'pg_trgm'
    source: 'user-override'
  }
}

// max_connections is intentionally left at the B1ms default (50). The
// 2026-05-06T13-14Z Sev2 was caused by stacked active Container App
// revisions (each pinned to minReplicas=2) blowing past the 50-slot
// ceiling, not by genuine load. The structural fix is in publish-image.yml
// (auto-deactivate stale revisions after promote) plus the 5+5 SQLAlchemy
// pool cap in shared/database.py — together they bound steady-state at
// ~30 connections.

output serverFqdn string = postgresServer.properties.fullyQualifiedDomainName
output serverName string = postgresServer.name
output databaseName string = database.name
