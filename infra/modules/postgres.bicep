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

// ── max_connections management (replica-count formula) ──
@description('When true, Bicep manages max_connections via the replica-count formula below. When false (default), max_connections is left at the B1ms default (50) and hand-tuned out of band — the historical behavior for all envs.')
param manageMaxConnections bool = false
@description('Maximum CMS container-app replicas (mirrors containerApps cmsMaxReplicas). Each CMS replica runs a single uvicorn process with a SQLAlchemy pool capped at pool_size+max_overflow = 10 connections.')
@minValue(1)
param cmsMaxReplicas int = 2
@description('Connection budget per CMS replica used by the max_connections formula. Sized for ~10 steady pool conns/replica plus rolling-deploy overlap (old+new revision briefly co-resident), migration transients, and monitoring headroom.')
@minValue(10)
param connectionsPerReplica int = 35
@description('Fixed connection reserve in the max_connections formula for the MCP app, the worker job, admin/migration sessions, and monitoring — independent of CMS replica count.')
@minValue(0)
param baseConnections int = 30

// max_connections = baseConnections + connectionsPerReplica * cmsMaxReplicas.
// At the current N=2 this evaluates to 30 + 35*2 = 100, matching the value
// dev was hand-bumped to. Scaling cmsMaxReplicas auto-raises the ceiling so
// the DB never has to be hand-tuned per replica-count change again.
var computedMaxConnections = baseConnections + (connectionsPerReplica * cmsMaxReplicas)

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
//
// dependsOn sslConfig is load-bearing: Azure Postgres Flexible Server
// serialises all `configurations` writes on the server, and Bicep would
// otherwise dispatch the two sibling configuration resources in parallel.
// When that happens the loser fails with `ServerIsBusy` ("Cannot complete
// operation while server '<name>' is busy processing another operation"),
// which has tripped both first-bootstrap and routine redeploys on the
// Goodwill dev B1ms SKU. The explicit chain forces sequential apply so
// the race cannot occur on any SKU.
resource extensionsConfig 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = {
  parent: postgresServer
  name: 'azure.extensions'
  properties: {
    value: 'pg_trgm'
    source: 'user-override'
  }
  dependsOn: [
    sslConfig
  ]
}

// max_connections management.
//
// Historically (and still, for any env where manageMaxConnections=false)
// this was left at the B1ms default (50). The 2026-05-06T13-14Z Sev2 was
// caused by stacked active Container App revisions (each pinned to
// minReplicas=2) blowing past the 50-slot ceiling, not by genuine load.
// The structural fix is in publish-image.yml (auto-deactivate stale
// revisions after promote) plus the 5+5 SQLAlchemy pool cap in
// shared/database.py — together they bound steady-state at ~30 conns.
//
// When manageMaxConnections=true (dev), Bicep instead pins max_connections
// to a replica-count formula (see computedMaxConnections above) so the DB
// ceiling tracks cmsMaxReplicas automatically and never needs hand-tuning
// when we scale out. At the current N=2 the formula yields 100 — the value
// dev was already bumped to — so the first managed deploy is a no-op write
// and does NOT trigger a server restart. Note: max_connections is a STATIC
// Postgres parameter, so any FUTURE change to the computed value (e.g.
// raising cmsMaxReplicas) WILL require a server restart on that deploy.
//
// dependsOn extensionsConfig is load-bearing for the same reason sslConfig
// chains into extensionsConfig: Azure Postgres Flexible Server serialises
// all `configurations` writes on the server. A parallel apply loses with
// `ServerIsBusy`. The explicit chain forces sequential apply.
resource maxConnectionsConfig 'Microsoft.DBforPostgreSQL/flexibleServers/configurations@2024-08-01' = if (manageMaxConnections) {
  parent: postgresServer
  name: 'max_connections'
  properties: {
    value: string(computedMaxConnections)
    source: 'user-override'
  }
  dependsOn: [
    extensionsConfig
  ]
}

output serverFqdn string = postgresServer.properties.fullyQualifiedDomainName
output serverName string = postgresServer.name
output databaseName string = database.name
