using '../main.bicep'

// ──────────────────────────────────────────────────────────────
// goodwill-dev.bicepparam — Goodwill DEV environment parameter values
//
// Deployed by .github/workflows/deploy-goodwill-dev.yml on every
// successful "Publish & Deploy" run on main (auto-deploy).  Gated by
// the `seattle-goodwill-dev` GH environment.
//
// This is a sibling of goodwill.bicepparam (the Goodwill PROD env).
// It targets resource group `agoragw-cms-dev-rg` in the same Goodwill
// tenant/subscription.  Resource names are scoped with the
// `agoragwdev` prefix so they cannot collide with prod's `agoragw-*`.
//
// Sizing: dev-sized container apps (matches infra/parameters/dev.bicepparam):
//   cms    : 0.5 vCPU / 1Gi
//   mcp    : 0.25 vCPU / 0.5Gi (main.bicep default for mcp at this scale)
//   worker : 1.0 vCPU / 2Gi
//
// Region: westus — matches the pre-created agoragw-cms-dev-rg.
// (Goodwill prod uses westus2; dev is in westus for cost/region
// flexibility. westus has full Container Apps + Postgres Flexible
// + Web PubSub coverage so all bicep modules apply unchanged.)
//
// Manual deploy (rare — prefer the workflow):
//   az deployment group create \
//     --resource-group agoragw-cms-dev-rg \
//     --template-file infra/main.bicep \
//     --parameters infra/parameters/goodwill-dev.bicepparam \
//     --parameters postgresAdminPassword='<secure>' \
//                  cmsSecretKey='<secure>' \
//                  cmsAdminPassword='<secure>' \
//                  adminPrincipalId='<entra-oid-in-goodwill-tenant>'
//
// adminPrincipalId is intentionally NOT set here.  It must be supplied
// at deploy time (the workflow passes it from the seattle-goodwill-dev
// environment's ADMIN_PRINCIPAL_ID secret).
// ──────────────────────────────────────────────────────────────

param prefix = 'agoragwdev'
param location = 'westus'

param postgresAdminLogin = 'agoraadmin'
param cmsAdminUsername = 'admin'

// Smaller container sizing for dev (mirrors dev.bicepparam):
param cmsCpu = '0.5'
param cmsMemory = '1Gi'
param workerCpu = '1.0'
param workerMemory = '2Gi'

// Use the AppInsights standard availability test against /healthz as
// the CMS heartbeat signal instead of the legacy request-count rule.
// Idle dev envs (no devices, no users) otherwise trip the legacy
// rule constantly because the OTel SDK filters probe traffic before
// ingestion.  Dev is the canary for this approach; once validated
// here we'll flip the default for the other envs too.
param useSyntheticHeartbeat = true

// Postgres max_connections management is DISABLED here.
//
// The replica-count formula (postgres.bicep: max_connections =
// 30 + 35*cmsMaxReplicas) was enabled on the assumption that re-writing a
// static PG parameter to its current value is a no-op that does not restart
// the server. That assumption is wrong: Azure Postgres Flexible Server
// restarts the server whenever a deployment APPLIES a restart-required
// (static) parameter, regardless of whether the value changed. Because the
// configurations resource is in the per-deploy bicep path, every dev deploy
// re-applied max_connections and restarted PG mid-rollout — the new CMS
// revision booted while the DB was bouncing, could not connect, and hung
// (verify saw HTTP 000000 for the full window). This durably broke every
// dev deploy after PR #789.
//
// Dev's max_connections is already at the desired ceiling (100, written by
// the first failed managed deploy), so leaving it unmanaged keeps that value
// while removing the per-deploy restart. Re-introducing auto-derivation
// later must move the static-param write OUT of the per-deploy path (e.g. a
// separate, manually-triggered infra job run only when cmsMaxReplicas
// changes, with an intentional restart window).
param manageMaxConnections = false

// Opt this environment into the Assistant feature backend.
// Phase 1: dev only. Prod opts in after the dev pilot validates the
// budget caps + approval UX.
param deployAzureOpenAI = true
// westus has GPT-4o quota out of the box; pin chat model + version
// so deploys are reproducible regardless of Azure's "latest" drift.
param azureOpenAIRegion = 'westus'
param azureOpenAIChatModel = 'gpt-4o'
param azureOpenAIChatModelVersion = '2024-11-20'
param azureOpenAIChatCapacity = 30

// Secure params — passed via the deploy-goodwill-dev workflow, never commit values:
// param postgresAdminPassword = '<set-via-cli>'
// param cmsSecretKey = '<set-via-cli>'
// param cmsAdminPassword = '<set-via-cli>'
// param adminPrincipalId = '<set-via-cli>'

// Container images — set by the deploy-goodwill-dev workflow via
// --parameters overrides. Dev pins by tag and tracks whatever was
// just published to main:
// param cmsImage = 'ghcr.io/sslivins/agora-cms:<version>'
// param mcpImage = 'ghcr.io/sslivins/agora-cms-mcp:<version>'
// param workerImage = 'ghcr.io/sslivins/agora-worker:<version>'
