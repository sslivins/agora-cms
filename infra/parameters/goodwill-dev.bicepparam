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

// Postgres max_connections is Bicep-managed in dev via the replica-count
// formula in postgres.bicep: max_connections = 30 + 35*cmsMaxReplicas.
// At the current cmsMaxReplicas=2 this is 100 — exactly the value dev was
// hand-bumped to — so enabling this is a no-op write today (no PG restart).
// Scaling cmsMaxReplicas later auto-raises the ceiling (that deploy WILL
// restart PG, since max_connections is a static parameter). Prod and all
// other envs leave manageMaxConnections=false (default 50) until reviewed.
param manageMaxConnections = true

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
